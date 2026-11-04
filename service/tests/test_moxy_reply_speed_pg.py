"""Moxy reply-latency changes: configurable reply pause, index-friendly fan history gate,
Moxy fast lane in the worker cycle and maintenance yielding. Disposable PG, registered fixture seams only."""
from datetime import timedelta
import threading

import pytest
from sqlalchemy import event as sa_event, select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from test_moxy_integrated_pg import setup, decision, model
from test_onlyfans_text_delivery_pg import TENANT, ACCOUNT
from db.models import AccountAiConfig, Message
from db.onlyfans_models import OfIngress, OfRecovery, OfWork
from db.onlyfans_moxy_models import MoxyEvent
from onlyfans_runtime.store import utcnow, fan_history_current
from onlyfans_runtime.text_delivery import SyntheticTextTransport
from moxy_replacement import runtime as moxy_runtime


# ---- (a) the artificial reply pause ---------------------------------------------------------

def test_reply_delay_defaults(monkeypatch):
    monkeypatch.delenv('MOXY_REPLY_DELAY_ACTIVE', raising=False)
    monkeypatch.delenv('MOXY_REPLY_DELAY_IDLE', raising=False)
    assert moxy_runtime.reply_delay_bounds(True) == (8.0, 25.0, 12.0)
    assert moxy_runtime.reply_delay_bounds(False) == (15.0, 40.0, 25.0)


def test_reply_delay_env_override(monkeypatch):
    monkeypatch.setenv('MOXY_REPLY_DELAY_ACTIVE', '6, 20, 10')
    monkeypatch.setenv('MOXY_REPLY_DELAY_IDLE', '10,30,15')
    assert moxy_runtime.reply_delay_bounds(True) == (6.0, 20.0, 10.0)
    assert moxy_runtime.reply_delay_bounds(False) == (10.0, 30.0, 15.0)


@pytest.mark.parametrize('bad', ['', 'x', '1,2', '8,25', '8,25,12,1', '4,25,12',   # below the 5 s floor
    '8,400,12', '30,25,12', '8,25,30', '8,25,5', 'nan,25,12', '8,inf,12'])
def test_reply_delay_bad_env_falls_back_to_default(monkeypatch, bad):
    monkeypatch.setenv('MOXY_REPLY_DELAY_ACTIVE', bad)
    monkeypatch.setenv('MOXY_REPLY_DELAY_IDLE', bad)
    assert moxy_runtime.reply_delay_bounds(True) == (8.0, 25.0, 12.0)
    assert moxy_runtime.reply_delay_bounds(False) == (15.0, 40.0, 25.0)


def test_reply_delay_is_never_instant(monkeypatch):
    import random
    monkeypatch.delenv('MOXY_REPLY_DELAY_ACTIVE', raising=False)
    monkeypatch.delenv('MOXY_REPLY_DELAY_IDLE', raising=False)
    for active in (True, False):
        low, high, mode = moxy_runtime.reply_delay_bounds(active)
        draws = [random.triangular(low, high, mode) for _ in range(2000)]
        assert min(draws) >= 8 and max(draws) <= 40


def paced_event(engine, service, store, key, mid):
    from test_onlyfans_foundation_pg import event
    store.record(TENANT, ACCOUNT, event(mid=mid, body='hey you'))
    store.drain(TENANT, ACCOUNT)
    return service.add_event(TENANT, ACCOUNT, 101, 'turn', key, {'source_message_id': mid})


def test_enqueue_uses_new_pause_idle_then_active(database, monkeypatch):
    engine, store, service, authority, claim, settings = setup(database, monkeypatch)
    monkeypatch.delenv('MOXY_REPLY_DELAY_ACTIVE', raising=False)
    monkeypatch.delenv('MOXY_REPLY_DELAY_IDLE', raising=False)
    with Session(engine) as s, s.begin():
        cfg = s.get(AccountAiConfig, ACCOUNT)
        cfg.ai_chatter_config_json = '{"native_reply_pacing_enabled": true}'
    seen = []
    real = moxy_runtime.__dict__.get('reply_delay_bounds')
    import random
    monkeypatch.setattr(random, 'triangular', lambda low, high, mode: seen.append((low, high, mode)) or low)
    idle = paced_event(engine, service, store, 'speed-idle', 2)
    with Session(engine) as s:
        e = s.get(MoxyEvent, (ACCOUNT, idle))
        received = s.get(Message, (ACCOUNT, 101, 2)).ingested_at
        assert e.not_before == received + timedelta(seconds=15)
    # A finished reply to the same fan in the last five minutes makes the conversation active.
    with Session(engine) as s, s.begin():
        s.get(MoxyEvent, (ACCOUNT, idle)).status = 'done'
    active = paced_event(engine, service, store, 'speed-active', 3)
    with Session(engine) as s:
        e = s.get(MoxyEvent, (ACCOUNT, active))
        received = s.get(Message, (ACCOUNT, 101, 3)).ingested_at
        assert e.not_before == received + timedelta(seconds=8)
    assert seen == [(15.0, 40.0, 25.0), (8.0, 25.0, 12.0)]
    store.close()


def test_enqueue_without_pacing_switch_has_no_pause(database, monkeypatch):
    engine, store, service, authority, claim, settings = setup(database, monkeypatch)
    ident = paced_event(engine, service, store, 'speed-off', 2)
    with Session(engine) as s:
        assert s.get(MoxyEvent, (ACCOUNT, ident)).not_before is None
    store.close()


# ---- (b)/(c) the fan history gate no longer scans the whole ingress table ---------------------

def test_fan_history_gate_semantics_and_index_friendly_sql(database, monkeypatch):
    engine, store, service, authority, claim, settings = setup(database, monkeypatch)
    monkeypatch.setenv('ONLYFANS_FAN_CURRENT_OVERRIDE', ACCOUNT + ':101')
    with Session(engine) as s, s.begin():
        s.merge(OfRecovery(account_id=ACCOUNT, fan_id=101, status='complete'))
        binding = s.scalar(select(OfIngress.binding_id).where(OfIngress.account_id == ACCOUNT).limit(1))
    statements = []
    @sa_event.listens_for(engine, 'before_cursor_execute')
    def capture(conn, cursor, statement, *a):
        statements.append(statement)
    try:
        with Session(engine) as s:
            assert fan_history_current(s, ACCOUNT, 101) is True
        for state, expected in (('pending', False), ('quarantined', False), ('applied', True)):
            with Session(engine) as s, s.begin():
                for row in s.scalars(select(OfIngress).where(OfIngress.account_id == ACCOUNT, OfIngress.fan_id == 101,
                        OfIngress.status.in_(('pending', 'quarantined')))):
                    row.status = 'applied'
                s.add(OfIngress(account_id=ACCOUNT, event_key='speed-' + state, binding_id=binding, fan_id=101,
                    message_id=900, source='catchup', payload='{}', status=state, received_at=utcnow()))
            with Session(engine) as s:
                assert fan_history_current(s, ACCOUNT, 101) is expected, state
        # Another fan's unapplied event never blocks this fan.
        with Session(engine) as s, s.begin():
            s.add(OfIngress(account_id=ACCOUNT, event_key='speed-other', binding_id=binding, fan_id=777,
                message_id=901, source='catchup', payload='{}', status='pending', received_at=utcnow()))
        with Session(engine) as s:
            assert fan_history_current(s, ACCOUNT, 101) is True
    finally:
        sa_event.remove(engine, 'before_cursor_execute', capture)
    gate = [q for q in statements if 'FROM of_ingress' in q and 'of_ingress.fan_id' in q and 'of_ingress.status' in q]
    assert gate and not any('!=' in q or '<>' in q for q in gate), gate
    store.close()


# ---- (d) fast lane and maintenance yield ------------------------------------------------------

def worker_for(store, m):
    from onlyfans_runtime.native_worker import NativeWorker, WorkerAccount
    return NativeWorker(store, TENANT, (WorkerAccount(ACCOUNT, m, SyntheticTextTransport(ACCOUNT)),))


def test_moxy_due_matches_ready_work_only(database, monkeypatch):
    from onlyfans_runtime.native_worker import moxy_due
    engine, store, service, authority, claim, settings = setup(database, monkeypatch)
    # Fixture claim is held with a live lease and nothing else is pending.
    assert moxy_due(engine) == set()
    future = service.add_event(TENANT, ACCOUNT, 101, 'turn', 'speed-future', {})
    with Session(engine) as s, s.begin():
        s.get(MoxyEvent, (ACCOUNT, future)).not_before = utcnow() + timedelta(minutes=5)
    assert moxy_due(engine) == set()
    ready_now = service.add_event(TENANT, ACCOUNT, 101, 'turn', 'speed-now', {})
    assert moxy_due(engine) == {ACCOUNT}
    assert moxy_due(engine, ['someone-else']) == set()
    with Session(engine) as s, s.begin():
        s.get(MoxyEvent, (ACCOUNT, ready_now)).status = 'done'
    assert moxy_due(engine) == set()
    # A waiting fan input past its quiet window is due; one still inside it is not.
    with Session(engine) as s, s.begin():
        work = s.get(OfWork, (ACCOUNT, 101))
        work.status, work.lease_token, work.lease_until = 'waiting', None, None
        work.due_at = utcnow() + timedelta(seconds=30)
    assert moxy_due(engine) == set()
    with Session(engine) as s, s.begin():
        s.get(OfWork, (ACCOUNT, 101)).due_at = utcnow() - timedelta(seconds=1)
    assert moxy_due(engine) == {ACCOUNT}
    store.close()


def test_fast_lane_serves_due_reply_and_respects_gap_and_switch(database, monkeypatch):
    from onlyfans_runtime.native_worker import CycleReport
    engine, store, service, authority, claim, settings = setup(database, monkeypatch)
    m = model(decision())
    runtime = worker_for(store, m)
    ident = service.add_event(TENANT, ACCOUNT, 101, 'scheduled', 'speed-fast', {})
    monkeypatch.setenv('ONLYFANS_MOXY_FAST_LANE', '0')
    report = CycleReport()
    runtime._moxy_fast_lane(report, threading.Event())
    with Session(engine) as s:
        assert s.get(MoxyEvent, (ACCOUNT, ident)).status == 'pending' and not m.calls
    monkeypatch.setenv('ONLYFANS_MOXY_FAST_LANE', '1')
    # The scheduled event is not a turn/welcome, so only a due turn triggers the lane.
    runtime._moxy_fast_lane(report, threading.Event())
    assert not m.calls
    turn = service.add_event(TENANT, ACCOUNT, 101, 'turn', 'speed-turn', {})
    with Session(engine) as s, s.begin():
        s.get(MoxyEvent, (ACCOUNT, ident)).status = 'done'
    runtime._moxy_fast_lane(report, threading.Event())
    assert len(m.calls) == 1
    with Session(engine) as s:
        assert s.get(MoxyEvent, (ACCOUNT, turn)).status in {'queued', 'done'}
    # Within the gap the same account is not served twice.
    again = service.add_event(TENANT, ACCOUNT, 101, 'turn', 'speed-turn-2', {})
    runtime._moxy_fast_lane(report, threading.Event())
    assert len(m.calls) == 1
    with Session(engine) as s:
        assert s.get(MoxyEvent, (ACCOUNT, again)).status == 'pending'
    store.close()


def test_step_runs_fast_lane_before_each_account_turn(database, monkeypatch):
    engine, store, service, authority, claim, settings = setup(database, monkeypatch)
    runtime = worker_for(store, model(decision()))
    calls = []
    monkeypatch.setattr(runtime, '_moxy_fast_lane', lambda report, stop, **kw: calls.append(1))
    runtime.step()
    assert calls == [1]
    store.close()


def test_maintenance_yields_to_waiting_moxy_reply_but_never_starves():
    import onlyfans_worker
    ran = []
    stop = threading.Event()
    passes = []
    def load_cohort(store, tenant, **kw):
        passes.append(1)
        if len(passes) >= 6:
            stop.set()
        return [], None, 0
    onlyfans_worker.run_loop(object(), 'tenant', stop, refresh_seconds=.01, load_cohort=load_cohort,
        vault_jobs=lambda store, tenant, **kw: ran.append(len(passes)), moxy_due=lambda: True)
    # Two passes yield to the waiting reply, the third runs the job, then the counter restarts.
    assert ran == [2, 5]
    ran.clear(); passes.clear(); stop.clear()
    onlyfans_worker.run_loop(object(), 'tenant', stop, refresh_seconds=.01, load_cohort=load_cohort,
        vault_jobs=lambda store, tenant, **kw: ran.append(len(passes)))
    assert len(ran) == 6          # no hook: unchanged, every pass
