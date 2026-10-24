"""FASTT's own nudge through the native runner (``--followups``), on the REAL ai_chatter engine.

Synthetic data, blocked sockets, no model: proves (no selling rule is changed here)
* flag off: every background job stays suppressed and TEXT_ONLY keeps nudges and cadence off;
* flag on: ai_chatter's own ``nudge_fan_ids`` job (scheduled after an unbought paid card) is persisted to the
  runner-owned ``native_pilot_followups`` table - never ``scheduled_jobs`` - and every other job kind is still suppressed;
* the stock ``_run_nudge`` runs through the engine with the job payload and sends FASTT's own plain line;
  a forged payload is refused; the cadence controller is inert (no burst cap skips the fan);
* a stale job (the fan replied) is a no-op in FASTT's own handler.
"""
import asyncio, json, logging, os, socket, sys, tempfile
from pathlib import Path
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ACCOUNT, FAN = 'of-900001', 900002
COUNT = 0


def check(cond, label):
    global COUNT
    assert cond, label
    COUNT += 1


async def exercise(tmp):
    from db.engine import engine, get_session
    from db.models import (Base, Account, AccountAiConfig, Fan, Employee, Message, CatalogItem, ContentOffer, ScheduledJob)
    from sqlalchemy import delete, select, func
    from native_pilot.content import ContentPlan
    from native_pilot.engine import NativeEngine, PilotBoundaryError, TEXT_ONLY, FOLLOWUPS
    from native_pilot.followups import FollowupQueue
    from native_pilot.ledger import Ledger
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    db_path = Path(tmp) / 'fixture.db'
    manifest = Path(tmp) / 'plan.json'
    manifest.write_text(json.dumps({'account_id': ACCOUNT, 'fan_id': FAN, 'items': [
        {'catalog_id': 1, 'kind': 'video', 'price_cents': 300,
         'media': [{'id': 1001, 'type': 'photo'}, {'id': 1101, 'type': 'video'}], 'preview_ids': [1001]}]}))
    plan = ContentPlan(manifest, ACCOUNT, FAN)
    async with get_session() as s:
        s.add(Account(id=ACCOUNT, nickname='Fixture'))
        s.add(Employee(id=1, display_name='Automation', is_active=True))
        s.add(Fan(account_id=ACCOUNT, fan_id=FAN))
        s.add(CatalogItem(id=1, account_id=ACCOUNT, label='Video 1', kind='video',
                          description_for_ai='One video. No other content details are verified.',
                          media_ids=json.dumps([1001, 1101]), preview_media_ids=json.dumps([1001]),
                          duration_sec=60, price_cents=300, is_free_teaser=False, enabled=True, pricing_mode='fixed'))
    state = {'script': [], 'mid': 100}
    sent, events = [], []
    queue = FollowupQueue(Ledger(db_path), ACCOUNT, datetime.utcnow)

    async def chat(**kw):
        if kw.get('response_format'):
            return SimpleNamespace(content='{}', parsed={})
        return SimpleNamespace(content=state['script'].pop(0) if state['script'] else 'haha tell me more', parsed=None)

    def send(fid, text, kw):
        term = plan.terms(kw)
        sent.append((text, kw))
        result = {'id': 50000 + len(sent), 'text': text, 'createdAt': datetime.utcnow().isoformat(), 'fromUser': {'id': 900001}}
        if term:
            result.update(media=[{'id': m, 'type': 'photo' if m < 1100 else 'video'} for m in kw['media_files']],
                          mediaCount=len(kw['media_files']), previews=kw['previews'],
                          price=term['price_cents'] / 100, lockedText=False)
            plan.evidence(result, term)
        return result

    def card(n=1):
        return f"ok one for you\nThis one is $3 to unlock.\n>>OFFER {n}"

    async def age():
        async with get_session() as s:
            f = await s.get(Fan, (ACCOUNT, FAN))
            f.automation_paused_until = None
            for m in (await s.execute(select(Message).where(Message.fan_id == FAN))).scalars():
                m.created_at = m.created_at - timedelta(seconds=90)

    async def fan_says(text='yes please'):
        await age()
        state['mid'] += 1
        async with get_session() as s:
            s.add(Message(account_id=ACCOUNT, fan_id=FAN, message_id=state['mid'], direction='in', body=text,
                          created_at=datetime.utcnow() - timedelta(seconds=30)))
        return state['mid']

    async def turn(*script, followups=None):
        state['script'] = list(script)
        mid = await fan_says()
        before = len(sent), len(events)
        with NativeEngine(ACCOUNT, FAN, chat=chat, send=send, emit=events.append, content=plan, followups=followups) as native:
            stats = await native.run(mid)
        assert not stats.get('errors'), stats
        return stats, sent[before[0]:], events[before[1]:], mid

    def rows(sql, args=()):
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(db_path)) as con, con:
            return con.execute(sql, args).fetchall()

    async def jobs():
        async with get_session() as s:
            return (await s.execute(select(func.count()).select_from(ScheduledJob))).scalar()

    async def reset():
        async with get_session() as s:
            await s.execute(delete(ContentOffer))
        rows('DELETE FROM native_pilot_followups') if rows("SELECT 1 FROM sqlite_master WHERE name='native_pilot_followups'") else None

    # 1. Flag off: stock suppression, TEXT_ONLY config, nothing persisted anywhere.
    with NativeEngine(ACCOUNT, FAN, chat=chat, send=send, emit=events.append, content=plan) as native:
        cfg = await native.native._load_config(ACCOUNT)
        check(cfg['nudge_enabled'] is False and cfg['cadence_enabled'] is False, 'off: nudge and cadence stay off')
    stats, out, ev, mid = await turn(card())
    check(stats['offers_made'] == 1, 'off: the card turn runs as before')
    check(not [e for e in ev if e['event'] == 'native-followup-queued'] and await jobs() == 0, 'off: no job queued anywhere')
    check(not rows("SELECT 1 FROM sqlite_master WHERE name='native_pilot_followups'") or not rows('SELECT 1 FROM native_pilot_followups'),
          'off: no follow-up row')
    await reset()

    # 2. Flag on: the config is FASTT's stock nudge with an inert cadence; the card turn queues the one job.
    with NativeEngine(ACCOUNT, FAN, chat=chat, send=send, emit=events.append, content=plan, followups=queue) as native:
        cfg = await native.native._load_config(ACCOUNT)
        check(all(cfg[k] == v for k, v in FOLLOWUPS.items()) and cfg['cadence_enabled'] and cfg['nudge_enabled'], 'on: nudge + inert cadence')
        check(all(cfg[k] == v for k, v in TEXT_ONLY.items() if k not in FOLLOWUPS), 'on: everything else stays TEXT_ONLY')
        check(cfg['nudge_after_minutes'] == 15, 'on: FASTT default delay')
    stats, out, ev, mid = await turn(card(), followups=queue)
    check(stats['offers_made'] == 1 and len(out) == 1 and out[0][1], 'on: the paid card goes out as before')
    queued = [e for e in ev if e['event'] == 'native-followup-queued']
    check(len(queued) == 1 and queued[0]['kind'] == 'nudge' and queued[0]['fan_id'] == FAN, 'on: nudge queued')
    job = rows('SELECT account_id,fan_id,kind,payload_json,run_at,last_in_id,state FROM native_pilot_followups')
    check(len(job) == 1 and job[0][:4] == (ACCOUNT, FAN, 'nudge', '{"nudge_fan_ids":[%d]}' % FAN) and job[0][5] == mid and job[0][6] == 'queued', job)
    delay = datetime.fromisoformat(job[0][4]) - datetime.utcnow()
    check(timedelta(minutes=14) < delay <= timedelta(minutes=15, seconds=5), 'on: due after nudge_after_minutes')
    check(await jobs() == 0, 'on: the legacy scheduled_jobs table is never written')

    # 3. Every other job kind stays suppressed, and a nudge for another fan or with extra keys is not kept.
    with NativeEngine(ACCOUNT, FAN, chat=chat, send=send, emit=events.append, content=plan, followups=queue) as native:
        before = len(events)
        run_at = datetime.utcnow() + timedelta(minutes=15)
        for kind, kw in (('ai_chatter', {'payload': {'post_buy': 3}}), ('ai_chatter', {'payload': {'aftercare': 3}}),
                         ('make_right', {'payload': {'hard_decline': {'fan_id': FAN}}}), ('gen_info', {'payload': {'force_ids': [FAN]}}),
                         ('ai_chatter', {'payload': {'nudge_fan_ids': [FAN + 1]}}),
                         ('ai_chatter', {'payload': {'nudge_fan_ids': [FAN], 'only_fan_ids': [FAN]}}),
                         ('ai_chatter', {'payload': {'only_fan_ids': [FAN], 'rhythm_resume': True}})):
            check(await native.enqueue_job(ACCOUNT, kind, run_at=run_at, **kw) is None, kind)
        check(len([e for e in events[before:] if e['event'] == 'native-background-job-suppressed']) == 7, 'all seven suppressed')
        check(not [e for e in events[before:] if e['event'] == 'native-followup-queued'], 'none queued')
    check(len(rows('SELECT 1 FROM native_pilot_followups')) == 1 and await jobs() == 0, 'still exactly the one nudge job')
    with NativeEngine(ACCOUNT, FAN, chat=chat, send=send, emit=events.append, content=plan, followups=queue) as native:
        await native.enqueue_job(ACCOUNT, 'ai_chatter', payload={'nudge_fan_ids': [FAN]}, run_at=run_at)
    check(rows('SELECT state,reason FROM native_pilot_followups ORDER BY created_at,job_id') in
          ([('dropped', 'superseded'), ('queued', None)], [('queued', None), ('dropped', 'superseded')]),
          'a newer nudge for the same fan supersedes the queued one: one live job per fan')
    check(len(rows("SELECT 1 FROM native_pilot_followups WHERE state='queued'")) == 1, 'exactly one live job')

    # 4. The stock handler runs through the engine with the job payload: FASTT's own plain line, cap-free text.
    with NativeEngine(ACCOUNT, FAN, chat=chat, send=send, emit=events.append, content=plan) as native:
        for bad in ({'nudge_fan_ids': [FAN]}, {'only_fan_ids': [FAN]}):
            try:
                await native.run(7, payload=bad)
                check(False, 'a job payload without the follow-up queue must be refused')
            except PilotBoundaryError as exc:
                check(str(exc) == 'forbidden-job-payload', 'refused')
    with NativeEngine(ACCOUNT, FAN, chat=chat, send=send, emit=events.append, content=plan, followups=queue) as native:
        for bad in ({'nudge_fan_ids': [FAN + 1]}, {'nudge_fan_ids': [FAN], 'max_replies': 9}, {'only_fan_ids': [FAN]}, {}):
            try:
                await native.run(7, payload=bad)
                check(False, 'forged payload')
            except PilotBoundaryError as exc:
                check(str(exc) == 'forbidden-job-payload', 'refused')
    await age()
    n0 = len(sent)
    with NativeEngine(ACCOUNT, FAN, chat=chat, send=send, emit=events.append, content=plan, followups=queue) as native:
        stats = await native.run(8, payload={'nudge_fan_ids': [FAN]})
    check(stats.get('nudged') == 1 and len(sent) == n0 + 1 and not sent[-1][1] and sent[-1][0].strip(), stats)
    last = rows("SELECT direction,automation_kind,price_cents,media_count FROM messages WHERE body=? ORDER BY message_id DESC", (sent[-1][0],))
    check(last and last[0] == ('out', 'ai_chatter', 0, 0), 'the nudge is stored as a plain AI row')

    # 5. FASTT's own handler drops a stale nudge: the fan replied first.
    await fan_says('wait what')
    n1 = len(sent)
    with NativeEngine(ACCOUNT, FAN, chat=chat, send=send, emit=events.append, content=plan, followups=queue) as native:
        stats = await native.run(9, payload={'nudge_fan_ids': [FAN]})
    check(stats.get('nudged') == 0 and stats.get('nudge_skipped') == 1 and len(sent) == n1, 'replied: stock handler sends nothing')

    # 6. The cadence controller is inert: sixteen AI replies (3 minutes apart) in the current burst never skip the fan.
    await reset()
    async with get_session() as s:
        for n in range(16):
            s.add(Message(account_id=ACCOUNT, fan_id=FAN, message_id=700 + n, direction='out', body=f'earlier {n}',
                          automation_kind='ai_chatter', sent_by_employee_id=1,
                          created_at=datetime.utcnow() - timedelta(minutes=55 - 3 * n)))
    from native_pilot import engine as engine_mod
    live = {**FOLLOWUPS, 'msg_limits_by_signal': {'baseline': 10, 'buying_signal': 20, 'no_signal': 5, 'pic_sent': 25}}
    with patch.object(engine_mod, 'FOLLOWUPS', live):
        control = await turn('lol ok', followups=queue)
    check(control[0].get('skipped_cadence') == 1 and control[0]['replies_sent'] == 0,
          'control: with FASTT default caps the same fan WOULD be skipped (the test can fail)')
    stats, out, ev, mid = await turn('lol ok', followups=queue)
    check(stats['replies_sent'] == 1 and stats.get('skipped_cadence', 0) == 0, 'burst caps never skip a fan with --followups')
    await engine.dispose()


def main():
    import dotenv
    dotenv.load_dotenv = lambda *a, **kw: False
    logging.disable(logging.CRITICAL)
    with tempfile.TemporaryDirectory(prefix='native-followups-fixture-') as tmp:
        with patch.dict(os.environ, {'DATABASE_URL': 'sqlite+aiosqlite:///' + (Path(tmp) / 'fixture.db').as_posix(), 'PYTHON_DOTENV_DISABLED': '1'}, clear=True):
            async def run():
                def deny(*a, **kw): raise AssertionError('network forbidden')
                with patch.object(socket.socket, 'connect', deny), patch.object(socket.socket, 'connect_ex', deny), patch.object(socket, 'create_connection', deny):
                    await exercise(tmp)
            asyncio.run(run())
    print(f'{COUNT} checks; all cases passed')


if __name__ == '__main__':
    main()
