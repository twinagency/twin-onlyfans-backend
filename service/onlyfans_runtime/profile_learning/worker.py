"""Resumable profile backfill, independent of live send/control workers.

Run as a separate supervised process against the same worker image and env:
python -m onlyfans_runtime.profile_learning.worker
Apply deploy/profile-learning/schema.sql first. No schema is created at startup.
"""
import asyncio
from datetime import datetime, timedelta
import json
import os
import signal
import threading

from sqlalchemy import exists, select, text, tuple_
from sqlalchemy.orm import Session
from db.models import Fan, FanProfile, Message, OfCreatorBinding
from db.onlyfans_models import OfIngress
from onlyfans_runtime.store import Store, Refused
from .evidence import digest, merge, summary, validate

INTERVAL = timedelta(hours=2)
PAGE = 40


def scoped(store, s, tenant, account, binding):
    runtime = store._runtime(s, tenant, account)
    if runtime.binding_id != binding:
        raise Refused('profile-binding-changed')
    return runtime


def seed(store, tenant):
    """Every fan gets a queue row, including silent fans, with no LLM cost."""
    with Session(store.engine) as s, s.begin():
        # Serialization also makes NOT EXISTS/INSERT safe with two schedulers.
        s.execute(text('SELECT pg_advisory_xact_lock(hashtext(:k))'), {'k': 'profile-seed:' + tenant})
        bindings = s.scalars(select(OfCreatorBinding).where(
            OfCreatorBinding.tenant_id == tenant, OfCreatorBinding.channel == 'onlyfans',
            OfCreatorBinding.revoked_at.is_(None))).all()
        for binding in bindings:
            scoped(store, s, tenant, binding.account_id, binding.id)
            params = {'tenant': tenant, 'account': binding.account_id, 'binding': binding.id}
            s.execute(text('''INSERT INTO of_profile_learning(tenant_id,account_id,binding_id,fan_id)
                VALUES (:tenant,:account,:binding,0) ON CONFLICT DO NOTHING'''), params)
            s.execute(text('''INSERT INTO of_profile_learning(tenant_id,account_id,binding_id,fan_id)
                SELECT :tenant,f.account_id,:binding,f.fan_id FROM fans f
                WHERE f.account_id=:account AND NOT EXISTS (
                    SELECT 1 FROM of_profile_learning p WHERE p.tenant_id=:tenant
                    AND p.account_id=f.account_id AND p.binding_id=:binding AND p.fan_id=f.fan_id)
                ORDER BY f.fan_id LIMIT 500 ON CONFLICT DO NOTHING'''), params)
            s.execute(text('''INSERT INTO fan_profiles(account_id,fan_id,message_count_at_gen,
                total_spend_at_gen_cents,notes_applied_successfully,last_generated_at)
                SELECT p.account_id,p.fan_id,0,0,false,CURRENT_TIMESTAMP FROM of_profile_learning p
                WHERE p.tenant_id=:tenant AND p.account_id=:account AND p.binding_id=:binding
                AND p.fan_id>0 ON CONFLICT DO NOTHING'''), params)


def source_page(store, job):
    with Session(store.engine) as s, s.begin():
        scoped(store, s, job['tenant_id'], job['account_id'], job['binding_id'])
        # Only current-binding applied ingress establishes historical provenance.
        proof = exists(select(OfIngress.event_key).where(
            OfIngress.account_id == Message.account_id, OfIngress.fan_id == Message.fan_id,
            OfIngress.message_id == Message.message_id, OfIngress.binding_id == job['binding_id'],
            OfIngress.status == 'applied'))
        query = select(Message).where(Message.account_id == job['account_id'], proof,
            tuple_(Message.ingested_at, Message.message_id, Message.fan_id) >
            tuple_(job['cursor_at'], job['cursor_message'], job['cursor_fan']))
        if job['fan_id']:
            query = query.where(Message.fan_id == job['fan_id'])
        else:
            query = query.where(Message.direction == 'out')
        rows = s.scalars(query.order_by(Message.ingested_at, Message.message_id, Message.fan_id)
                         .limit(PAGE + 1)).all()
        sources = {}
        for m in rows[:PAGE]:
            if m.is_unsent or not m.body.strip() or m.automation_kind:
                continue
            key = str(m.fan_id) + ':' + str(m.message_id)
            sources[key] = {'id': key, 'fan': m.fan_id, 'message': m.message_id,
                'text': m.body[:1400], 'at': m.created_at.isoformat(), 'direction': m.direction,
                'automation': m.automation_kind, 'digest': digest([m.body, m.is_unsent])}
        end = rows[min(PAGE, len(rows)) - 1] if rows else None
        cursor = (end.ingested_at, end.message_id, end.fan_id) if end else None
        return sources, cursor, len(rows) > PAGE, min(PAGE, len(rows))


def publish_creator(store, job, document):
    from onlyfans_runtime.launch_personality.workflow import PersonalityWorkflow
    wf = PersonalityWorkflow(store)
    current = wf.get(job['tenant_id'], job['account_id'])
    if current['bindingId'] != job['binding_id']:
        raise Refused('profile-binding-changed')
    if (current.get('document') or {}).get('automaticProfile') == document:
        return
    # Human review stays intact. An operator can import again to see new evidence.
    if current.get('document') and current['document'].get('review'):
        return
    def build(s, runtime, config, previous):
        if previous is not None:
            old = json.loads(previous.document_json)
            if old.get('review'):
                raise Refused('profile-review-in-progress')
        else:
            old = {'draft': {'creatorFacts': [], 'writingStyle': [], 'operationalInstructions': [],
                           'evidence': [], 'coverage': {}}, 'review': {}, 'source': 'automatic_onlyfans_history'}
        old['automaticProfile'] = document
        old['draft']['suggestedPersona'] = {'text': summary(document.get('facts', []), creator=True),
            'attribution': 'unknown', 'evidenceIds': [], 'selected': False, 'requiresReview': True,
            'basis': 'observed_account_claims', 'warning': 'Account messages may be agency-authored.'}
        return old, 'draft'
    wf._change(job['tenant_id'], job['account_id'], current['revision'],
        digest(['automatic-profile', job['binding_id'], document]),
        'worker-import:' + digest(['profile-learning', job['binding_id']]),
        {'action': 'automatic_profile', 'documentHash': digest(document)}, build,
        expected_binding_id=job['binding_id'])


async def step(store, tenant, stop, *, extractor=None):
    if extractor is None:
        from .model import extract
        extractor = extract
    # Session-level lock is held across the model call on a dedicated connection;
    # transactions are short. A crashed process releases the lock automatically.
    with store.engine.connect() as lock:
        key = 'profile-learning:' + tenant
        if not lock.execute(text('SELECT pg_try_advisory_lock(hashtext(:k))'), {'k': key}).scalar():
            return False
        lock.commit()
        try:
            with Session(store.engine) as s:
                job = s.execute(text('''SELECT p.* FROM of_profile_learning p
                    JOIN of_creator_bindings b ON b.id=p.binding_id AND b.account_id=p.account_id
                    WHERE p.tenant_id=:tenant AND b.tenant_id=:tenant AND b.revoked_at IS NULL
                    AND p.next_at<=CURRENT_TIMESTAMP ORDER BY p.next_at,p.account_id,p.fan_id LIMIT 1'''),
                    {'tenant': tenant}).mappings().first()
                if job is None or stop.is_set():
                    return False
                job = dict(job)
                if isinstance(job['cursor_at'], str):
                    job['cursor_at'] = datetime.fromisoformat(job['cursor_at'])
            identity = {k: job[k] for k in ('tenant_id', 'account_id', 'binding_id', 'fan_id')}
            where = 'tenant_id=:tenant_id AND account_id=:account_id AND binding_id=:binding_id AND fan_id=:fan_id'
            try:
                sources, cursor, more, count = source_page(store, job)
                document = json.loads(job['document_json'])
                additions, call_id = [], None
                wanted = {k: v for k, v in sources.items()
                          if v['direction'] == ('in' if job['fan_id'] else 'out')}
                if wanted:
                    result, call_id = await extractor(store, tenant, job['account_id'], job['binding_id'],
                        job['fan_id'], wanted, creator=not bool(job['fan_id']))
                    additions = validate(result, wanted, creator=not bool(job['fan_id']))
                if stop.is_set():
                    return False
                with Session(store.engine) as s, s.begin():
                    scoped(store, s, tenant, job['account_id'], job['binding_id'])
                    # Check source edits/deletions after network work. Prior facts
                    # are revalidated too, so withdrawn evidence leaves summaries.
                    facts = merge(document.get('facts', []), additions)
                    checked = []
                    for fact in facts:
                        fan, mid = map(int, fact['source'].split(':'))
                        m = s.get(Message, (job['account_id'], fan, mid))
                        if m is not None and not m.is_unsent and digest([m.body, m.is_unsent]) == fact['digest']:
                            checked.append(fact)
                    document['facts'] = checked
                    document['coverage'] = 'Imported current-binding messages; provider history may still be syncing.'
                    if job['fan_id']:
                        from onlyfans_runtime.launch_history_memory.memory import locked_fields
                        locks = locked_fields(s, job['account_id'], job['fan_id'])
                        field_locks = {'name': {'real_name'}, 'age': {'his_age'},
                            'location': {'home_city', 'home_country'}, 'occupation': {'occupation'},
                            'interests': {'hobbies'}, 'preferences': {'fetishes'}}
                        checked = [f for f in checked if not locks & field_locks.get(f['field'], set())]
                        document['facts'] = checked
                        document['summary_locked'] = bool(locks & {'short_bio', 'bullet_points'})
                        profile = s.get(FanProfile, (job['account_id'], job['fan_id']))
                        if profile is None:
                            profile = FanProfile(account_id=job['account_id'], fan_id=job['fan_id'])
                            s.add(profile)
                        previous = document.get('owned_summary')
                        # Never overwrite a manual/external summary. Our own
                        # summary can update; protected typed/operator facts stay.
                        if ('short_bio' not in locks and 'bullet_points' not in locks
                                and (not profile.short_bio or profile.short_bio == previous)):
                            profile.short_bio = summary(checked)
                            profile.bullet_points = '\n'.join(f"{f['field']}: {f['value']} [{f['source']}]" for f in checked)
                            profile.last_generated_at = datetime.utcnow()
                            profile.generated_by_grok_call_id = call_id or profile.generated_by_grok_call_id
                            document['owned_summary'] = profile.short_bio
                    params = {**identity, 'doc': json.dumps(document, ensure_ascii=False), 'count': count,
                        'due': datetime.utcnow() + (timedelta(seconds=1) if more else INTERVAL)}
                    if cursor:
                        params.update(at=cursor[0], mid=cursor[1], cf=cursor[2])
                    else:
                        params.update(at=job['cursor_at'], mid=job['cursor_message'], cf=job['cursor_fan'])
                    s.execute(text(f'''UPDATE of_profile_learning SET document_json=:doc,
                        cursor_at=:at,cursor_message=:mid,cursor_fan=:cf,
                        processed_messages=processed_messages+:count,next_at=:due,
                        checked_at=CURRENT_TIMESTAMP,error_code=NULL WHERE {where}'''), params)
                if not job['fan_id']:
                    publish_creator(store, job, document)
                return True
            except Exception as exc:
                # Fixed public codes only, never credential-bearing exception text.
                allowed = {'model-credential-missing', 'model-credential-mismatch',
                           'profile-binding-changed', 'profile-model-usage-unavailable'}
                code = str(exc) if isinstance(exc, Refused) and str(exc) in allowed else 'profile-analysis-deferred'
                with Session(store.engine) as s, s.begin():
                    s.execute(text(f'''UPDATE of_profile_learning SET error_code=:error,
                        next_at=:due WHERE {where}'''), {**identity, 'error': code,
                        'due': datetime.utcnow() + timedelta(minutes=15)})
                return True
        finally:
            lock.execute(text('SELECT pg_advisory_unlock(hashtext(:k))'), {'k': key})
            lock.commit()


def main():
    tenant = os.environ.get('ONLYFANS_WORKER_TENANT', '')
    if os.environ.get('ONLYFANS_PROCESS_ROLE') != 'worker' or not tenant or tenant == 'deployment':
        raise SystemExit('Explicit worker role and tenant required')
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    store = Store(os.environ['DATABASE_URL'])
    try:
        while not stop.is_set():
            try:
                seed(store, tenant)
                worked = asyncio.run(step(store, tenant, stop))
            except Exception:
                print(json.dumps({'component': 'profile-learning', 'state': 'unavailable'}), flush=True)
                worked = False
            stop.wait(1 if worked else 30)
    finally:
        store.close()


if __name__ == '__main__':
    main()
