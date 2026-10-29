"""SQLite behavioral rehearsal; production advisory locks require PostgreSQL QA."""
import asyncio
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

os.environ.setdefault('TWIN_FASTT_CONFIG_ISOLATED', '1')
os.environ.setdefault('DATABASE_URL', 'sqlite+aiosqlite:///:memory:')
import onlyfans_runtime
onlyfans_runtime.__path__.insert(0, str(Path(__file__).parents[1] / 'onlyfans_runtime'))
from db.models import Account, Fan, FanProfile, Message, OfCreatorBinding
from db.onlyfans_models import OfIngress, OfRuntime
from onlyfans_runtime.store import Store
from onlyfans_runtime.launch_history_memory.models import MemoryCorrection
from onlyfans_runtime.profile_learning import worker

NOW = datetime(2026, 1, 1)


@pytest.fixture
def store():
    engine = create_engine('sqlite://', poolclass=StaticPool)
    @event.listens_for(engine, 'connect')
    def functions(db, _):
        import hashlib
        db.create_function('md5',1,lambda x:hashlib.md5(x.encode()).hexdigest())
        db.create_function('hashtext', 1, lambda x: 1)
        for name in ('pg_advisory_xact_lock', 'pg_try_advisory_lock', 'pg_advisory_unlock'):
            db.create_function(name, 1, lambda _: 1)
    for table in (Account, OfCreatorBinding, OfRuntime, Fan, FanProfile, Message, OfIngress, MemoryCorrection):
        table.__table__.create(engine)
    schema = Path(__file__).parents[2] / 'deploy/profile-learning/schema.sql'
    with engine.begin() as c:
        ddl = '\n'.join(line.split('--', 1)[0] for line in schema.read_text().splitlines())
        for statement in ddl.split(';'):
            if statement.strip():
                c.exec_driver_sql(statement)
    with Session(engine) as s, s.begin():
        s.add(Account(id='of-1'))
        s.add(OfCreatorBinding(id='binding', account_id='of-1', tenant_id='tenant', creator_id='creator'))
        s.add(OfRuntime(account_id='of-1', binding_id='binding', activated_at=NOW))
        s.add_all([Fan(account_id='of-1', fan_id=i) for i in (1, 2, 3)])
    yield SimpleNamespace(engine=engine, _runtime=Store._runtime)
    engine.dispose()


def add_message(store, mid=1, fan=1, body='I enjoy tennis', binding='binding', **kw):
    with Session(store.engine) as s, s.begin():
        s.add(Message(account_id='of-1', fan_id=fan, message_id=mid, direction='in', body=body,
                      created_at=kw.get('created_at', NOW), ingested_at=kw.get('ingested_at', NOW)))
        s.add(OfIngress(account_id='of-1', event_key=str(fan)+':'+str(mid), binding_id=binding,
            fan_id=fan, message_id=mid, source='poll', payload='{}', status='applied', received_at=NOW))


def fan_due(store, fan=1):
    worker.seed(store, 'tenant')
    with store.engine.begin() as c:
        c.execute(text('UPDATE of_profile_learning SET next_at=:future'), {'future': datetime.utcnow()+timedelta(days=1)})
        c.execute(text('UPDATE of_profile_learning SET next_at=:past WHERE fan_id=:fan'), {'past': NOW, 'fan': fan})


async def extractor(store, tenant, account, binding, fan, sources, **kw):
    return {'facts': [{'field': 'interests', 'value': v['text'], 'quote': v['text'], 'source': k}
                      for k, v in list(sources.items())[:20]]}, None


def run(store, fn=extractor):
    return asyncio.run(worker.step(store, 'tenant', threading.Event(), extractor=fn))


def state(store, fan=1):
    with store.engine.connect() as c:
        row = c.execute(text('SELECT * FROM of_profile_learning WHERE fan_id=:f'), {'f': fan}).mappings().one()
        doc=json.loads(row['document_json'])
        doc['facts']=[json.loads(r[0]) for r in c.execute(text('SELECT document_json FROM of_profile_memories WHERE fan_id=:f'),{'f':fan})]
        return {**row, 'document':doc}


def test_every_fan_including_silent_fans_gets_a_record(store):
    worker.seed(store, 'tenant')
    worker.seed(store, 'tenant')
    with Session(store.engine) as s:
        assert len(s.scalars(select(FanProfile)).all()) == 3
        assert s.execute(text('SELECT count(*) FROM of_profile_learning')).scalar() == 4


def test_no_personal_statement_is_still_processed(store):
    fan_due(store)
    assert run(store)
    assert state(store)['checked_at']
    assert state(store)['document']['facts'] == []


def test_backfill_and_incremental_update_include_late_old_history(store):
    add_message(store)
    fan_due(store)
    assert run(store)
    assert state(store)['processed_messages'] == 1
    add_message(store, mid=2, body='I enjoy music', created_at=NOW-timedelta(days=30), ingested_at=NOW+timedelta(hours=1))
    fan_due(store)
    assert run(store)
    assert state(store)['processed_messages'] == 2
    assert len(state(store)['document']['facts']) == 2


def test_other_binding_messages_are_excluded(store):
    add_message(store, binding='old-owner')
    fan_due(store)
    run(store)
    assert state(store)['processed_messages'] == 0
    assert state(store)['document']['facts'] == []


def test_source_change_during_extraction_is_refused(store):
    add_message(store)
    fan_due(store)
    async def changed(*args, **kwargs):
        result = await extractor(*args, **kwargs)
        with Session(store.engine) as s, s.begin():
            s.get(Message, ('of-1', 1, 1)).is_unsent = True
        return result
    run(store, changed)
    assert state(store)['document']['facts'] == []


def test_deleted_source_is_removed_on_refresh(store):
    add_message(store)
    fan_due(store)
    run(store)
    assert state(store)['document']['facts']
    with Session(store.engine) as s, s.begin():
        s.get(Message, ('of-1', 1, 1)).is_unsent = True
    fan_due(store)
    run(store)
    assert state(store)['document']['facts'] == []


def test_existing_manual_summary_is_preserved(store):
    add_message(store)
    fan_due(store)
    with Session(store.engine) as s, s.begin():
        s.get(FanProfile, ('of-1', 1)).short_bio = 'Operator summary'
    run(store)
    with Session(store.engine) as s:
        assert s.get(FanProfile, ('of-1', 1)).short_bio == 'Operator summary'


def test_model_failure_keeps_cursor_and_records_retry(store):
    add_message(store)
    fan_due(store)
    async def unavailable(*a, **kw):
        raise RuntimeError('private provider error must not be saved')
    run(store, unavailable)
    row = state(store)
    assert row['processed_messages'] == 0
    assert row['error_code'] == 'profile-analysis-deferred'


def test_page_limit_is_resumable_not_a_total_fan_limit(store):
    for i in range(1, worker.PAGE+3):
        add_message(store, mid=i)
    fan_due(store)
    run(store)
    assert state(store)['processed_messages'] == worker.PAGE
    fan_due(store)
    run(store)
    assert state(store)['processed_messages'] == worker.PAGE+2


def test_incremental_summary_changes_without_reprocessing_old_messages(store):
    add_message(store)
    fan_due(store)
    run(store)
    with Session(store.engine) as s:
        assert 'tennis' in state(store)['document']['summary']
    add_message(store, mid=2, body='I enjoy music', ingested_at=NOW+timedelta(hours=2))
    fan_due(store)
    run(store)
    with Session(store.engine) as s:
        value = state(store)['document']['summary']
        assert 'tennis' in value and 'music' in value
    checked = datetime.fromisoformat(state(store)['checked_at'])
    due = datetime.fromisoformat(state(store)['next_at'])
    assert 7195 <= (due-checked).total_seconds() <= 7205


def test_rebinding_during_model_call_cannot_commit_facts(store):
    add_message(store)
    fan_due(store)
    async def rebound(*args, **kwargs):
        result = await extractor(*args, **kwargs)
        with Session(store.engine) as s, s.begin():
            s.get(OfCreatorBinding, 'binding').revoked_at = NOW
        return result
    run(store, rebound)
    assert state(store)['processed_messages'] == 0
    assert not state(store)['document'].get('facts')


def test_operator_fact_lock_excludes_conflicting_generated_fact(store):
    add_message(store)
    fan_due(store)
    with Session(store.engine) as s, s.begin():
        s.add(MemoryCorrection(account_id='of-1', fan_id=1, command_key='lock',
            field='hobbies', operation='set', value='Operator value', before_json='{}',
            request_hash='hash', revision=1, actor='operator', reason='Correction', created_at=NOW))
    run(store)
    assert state(store)['document']['facts'] == []


def test_seed_continues_beyond_first_five_hundred_fans(store):
    with Session(store.engine) as s, s.begin():
        s.add_all(Fan(account_id='of-1', fan_id=i) for i in range(4, 1002))
    for _ in range(3):
        worker.seed(store, 'tenant')
    with store.engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM of_profile_learning WHERE fan_id>0')).scalar() == 1001


def test_entire_silent_roster_is_processed_beyond_discovery_page(store):
    with Session(store.engine) as s,s.begin():
        s.add_all(Fan(account_id='of-1',fan_id=i) for i in range(4,506))
    for _ in range(2): worker.seed(store,'tenant')
    with store.engine.begin() as c:
        c.execute(text('UPDATE of_profile_learning SET next_at=:future WHERE fan_id=0'),{'future':datetime.utcnow()+timedelta(days=1)})
    for _ in range(505): assert run(store)
    with store.engine.connect() as c:
        assert c.scalar(text('SELECT count(*) FROM of_profile_learning WHERE fan_id>0 AND checked_at IS NOT NULL AND error_code IS NULL'))==505


# ---- fan-memory throughput release (2026-10-29) ----
def add_outgoing(store, mid, fan=1, body='Mass message: new set tonight'):
    with Session(store.engine) as s, s.begin():
        s.add(Message(account_id='of-1', fan_id=fan, message_id=mid, direction='out', body=body,
                      created_at=NOW, ingested_at=NOW))
        s.add(OfIngress(account_id='of-1', event_key=f'o{fan}:{mid}', binding_id='binding', fan_id=fan,
            message_id=mid, source='poll', payload='{}', status='applied', received_at=NOW))


def test_fan_job_never_spends_steps_on_outgoing_messages(store):
    for mid in range(100, 100+worker.PAGE*3): add_outgoing(store, mid)
    add_message(store, mid=1)
    fan_due(store)
    assert run(store)
    row = state(store)
    # One step completes the fan: the 120 outgoing mass messages are not receipted at all.
    assert row['processed_messages'] == 1 and row['document']['history_complete'] is True
    with store.engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM of_profile_receipts WHERE direction='out'")).scalar() == 0


def test_claim_prefers_incoming_tiers_then_age(store):
    worker.seed(store, 'tenant')
    old = NOW
    with store.engine.begin() as c:
        c.execute(text('UPDATE of_profile_learning SET next_at=:t,priority=2'), {'t': old})
        c.execute(text('UPDATE of_profile_learning SET next_at=:t WHERE fan_id=3'), {'t': old-timedelta(days=1)})
        c.execute(text('UPDATE of_profile_learning SET priority=1 WHERE fan_id=2'))
        c.execute(text('UPDATE of_profile_learning SET priority=0,next_at=:t WHERE fan_id=1'), {'t': datetime.utcnow()-timedelta(seconds=1)})
    order = []
    for _ in range(3):
        job = worker.claim_job(store, 'tenant', threading.Event())
        order.append(job['fan_id'])
    assert order == [1, 2, 3]


def test_silent_fans_complete_in_bulk_without_a_model_and_stay_honest(store):
    with Session(store.engine) as s, s.begin():
        s.add_all(Fan(account_id='of-1', fan_id=i) for i in range(4, 60))
    add_message(store, mid=1, fan=1)           # has incoming: must not be bulk-completed
    for _ in range(2): worker.seed(store, 'tenant')
    with store.engine.begin() as c:
        c.execute(text('UPDATE of_profile_learning SET next_at=:t'), {'t': NOW})
        c.execute(text('UPDATE of_profile_learning SET priority=1 WHERE fan_id=1'))
    done = worker.finalize_silent(store, 'tenant', threading.Event(), limit=25)
    assert done == 25
    done += worker.finalize_silent(store, 'tenant', threading.Event(), limit=100)
    assert done == 58        # fans 2..59; fan 1 (incoming) and the creator draft are left to the normal path
    assert state(store, 1)['checked_at'] is None
    silent = state(store, 30)
    assert silent['checked_at'] and silent['document']['history_complete'] and silent['document']['memory_count'] == 0
    assert silent['error_code'] is None
    assert 86000 <= (datetime.fromisoformat(str(silent['next_at'])) - datetime.utcnow()).total_seconds() <= 86500
    assert worker.finalize_silent(store, 'tenant', threading.Event()) == 0
    assert state(store, 0)['checked_at'] is None


def test_new_incoming_message_after_silent_completion_is_still_learned(store):
    worker.seed(store, 'tenant')
    with store.engine.begin() as c: c.execute(text('UPDATE of_profile_learning SET next_at=:t'), {'t': NOW})
    worker.finalize_silent(store, 'tenant', threading.Event())
    add_message(store, mid=9, fan=2, body='I love hiking')
    fan_due(store, fan=2)
    assert run(store)
    assert state(store, 2)['processed_messages'] == 1 and state(store, 2)['document']['facts']


def test_lock_or_statement_timeout_is_not_a_failure_and_does_not_back_off(store):
    from sqlalchemy.exc import OperationalError
    class Timeout(Exception): pgcode = '55P03'
    add_message(store); fan_due(store)
    async def busy(*a, **kw): raise OperationalError('SELECT 1', {}, Timeout('canceling statement due to lock timeout'))
    run(store, busy)
    row = state(store)
    assert row['error_code'] is None and row['attempts'] == 0 and row['lease_token'] is None
    assert 0 < (datetime.fromisoformat(str(row['next_at'])) - datetime.utcnow()).total_seconds() <= 95


def test_missing_model_pauses_the_account_not_every_fan_and_silent_work_continues(store):
    from onlyfans_runtime.store import Refused
    with Session(store.engine) as s, s.begin():
        s.add_all(Fan(account_id='of-1', fan_id=i) for i in range(4, 8))
    add_message(store, mid=1, fan=1); add_message(store, mid=2, fan=2, body='I like golf')
    fan_due(store)
    with store.engine.begin() as c: c.execute(text('UPDATE of_profile_learning SET next_at=:t WHERE fan_id IN (1,2)'), {'t': NOW})
    async def nokey(*a, **kw): raise Refused('model-credential-missing')
    run(store, nokey)
    assert state(store, 1)['error_code'] == 'model-credential-missing'
    assert worker.claim_job(store, 'tenant', threading.Event()) is None        # account paused; fan 2 not hammered
    with store.engine.connect() as c:
        assert c.execute(text('SELECT hold_code FROM of_profile_accounts')).scalar() == 'model-credential-missing'
    assert state(store, 2)['error_code'] is None and state(store, 2)['checked_at'] is None   # still incomplete
    with store.engine.begin() as c: c.execute(text('UPDATE of_profile_learning SET next_at=:t WHERE fan_id>3'), {'t': NOW})
    assert worker.finalize_silent(store, 'tenant', threading.Event()) == 4       # no model needed
