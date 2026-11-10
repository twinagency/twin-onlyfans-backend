"""U5: the provider's length-limit verdict survives a restart at every persistence boundary (offline, disposable PG)."""
import multiprocessing
import os
import threading

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT
from test_engine_model_pg import PROFILE, config, ok, rows, with_model
from test_engine_simulated_journey_pg import incoming
from conversation_engine.engine import EngineService
from conversation_engine.model_client import ChatResult, FixtureChatClient, Usage
from conversation_engine.model_ledger import LENGTH_LIMITED, LENGTH_PENDING
from conversation_engine.model_runtime import ModelRuntime
from conversation_engine.writer_context import StaticCreatorProfiles
from db.onlyfans_engine_models import OfEngineDecision, OfEngineModelCall
from onlyfans_runtime.store import Refused

CUT = 'this reply was cut off midw'
GOOD = 'hey you, how was your day'
PINNED = dict(upstream_provider='fixture-host', upstream_reported_name='Fixture Host')


def answer(text, finish='stop', cost=500, pinned=False, usage=None):
    return ChatResult(text=ok(text), usage=usage or Usage(100, 10, cost), finish_reason=finish,
                      model='m/primary' if pinned else '', provider='Fixture Host' if pinned else '')


def decisions(database):
    with Session(database[1]) as s:
        return [d.body for d in s.scalars(select(OfEngineDecision))]


def crash_at(runtime, point):
    """Make the process 'die' (exception) at one named boundary of the model-call persistence sequence."""
    ledger = runtime.ledger
    name, when = {'after_settle': ('settle', 'after'), 'after_identity_note': ('note_identity', 'after'),
                  'before_finish_identity': ('finish_identity', 'before'), 'after_finish_identity': ('finish_identity', 'after'),
                  'before_set_outcome': ('set_outcome', 'before'), 'after_set_outcome': ('set_outcome', 'after')}[point]
    original = getattr(ledger, name)

    def wrapper(*a, **kw):
        if when == 'before':
            raise RuntimeError('injected crash ' + point)
        result = original(*a, **kw)
        raise RuntimeError('injected crash ' + point)
    setattr(ledger, name, wrapper)
    return lambda: setattr(ledger, name, original)


def restart(database, engine, client, cfg):
    """A brand-new worker (runtime + ledger, new worker id) built from the same database and provider client."""
    runtime = ModelRuntime(database[1], client, cfg, StaticCreatorProfiles({ACCOUNT: PROFILE}))
    engine.model = runtime
    return runtime


PLAIN_POINTS = ['after_settle', 'before_set_outcome', 'after_set_outcome']
PINNED_POINTS = ['after_settle', 'after_identity_note', 'before_finish_identity', 'after_finish_identity',
                 'before_set_outcome', 'after_set_outcome']
PINNED_STILL_UNVERIFIED = {'after_settle', 'after_identity_note', 'before_finish_identity'}


def _cases():
    return [(False, p) for p in PLAIN_POINTS] + [(True, p) for p in PINNED_POINTS]


@pytest.mark.parametrize('pinned,point', _cases())
def test_length_limited_response_is_never_delivered_after_interruption_at_each_boundary(database, pinned, point):
    cfg = config(**(PINNED if pinned else {}))
    store, authority, engine, runtime, client, _ = with_model(database, [answer(CUT, 'length', pinned=pinned),
                                                                          answer(GOOD, pinned=pinned)], cfg=cfg)
    try:
        incoming(store, 1, 'hello')
        crash_at(runtime, point)
        with pytest.raises(RuntimeError, match='injected crash ' + point):
            engine.plan(TENANT, ACCOUNT, FAN, 1)
        [saved] = rows(database)
        assert saved.response_text is not None and saved.outcome != 'verified_ok' and saved.outcome != 'ok'
        assert decisions(database) == [] and len(client.requests) == 1
        restart(database, engine, client, cfg)
        if pinned and point in PINNED_STILL_UNVERIFIED:
            # identity was not yet recorded: the existing protection hands it to a human; nothing is reused or sent
            assert saved.outcome == LENGTH_PENDING
            with pytest.raises(Refused, match='identity is unverified'):
                engine.plan(TENANT, ACCOUNT, FAN, 1)
            assert len(client.requests) == 1 and decisions(database) == []
        else:
            assert engine.plan(TENANT, ACCOUNT, FAN, 1)
            assert decisions(database) == [GOOD] and len(client.requests) == 2
            assert [r.outcome for r in rows(database)] == ['invalid', 'verified_ok' if pinned else 'ok']
    finally:
        store.close()


@pytest.mark.parametrize('pinned', [False, True])
def test_uninterrupted_control_matches_replay(database, pinned):
    cfg = config(**(PINNED if pinned else {}))
    store, authority, engine, runtime, client, _ = with_model(database, [answer(CUT, 'length', pinned=pinned),
                                                                          answer(GOOD, pinned=pinned)], cfg=cfg)
    try:
        incoming(store, 1, 'hello')
        assert engine.plan(TENANT, ACCOUNT, FAN, 1)
        assert decisions(database) == [GOOD] and len(client.requests) == 2
        assert [r.outcome for r in rows(database)] == ['invalid', 'verified_ok' if pinned else 'ok']
    finally:
        store.close()


@pytest.mark.parametrize('pinned,point', _cases())
def test_completed_response_replays_once_without_new_model_call(database, pinned, point):
    cfg = config(**(PINNED if pinned else {}))
    store, authority, engine, runtime, client, _ = with_model(database, [answer(GOOD, pinned=pinned),
                                                                          answer('must not be requested', pinned=pinned)], cfg=cfg)
    try:
        incoming(store, 1, 'hello')
        crash_at(runtime, point)
        with pytest.raises(RuntimeError, match='injected crash ' + point):
            engine.plan(TENANT, ACCOUNT, FAN, 1)
        restart(database, engine, client, cfg)
        if pinned and point in PINNED_STILL_UNVERIFIED:
            with pytest.raises(Refused, match='identity is unverified'):
                engine.plan(TENANT, ACCOUNT, FAN, 1)
            assert len(client.requests) == 1 and decisions(database) == []
        else:
            assert engine.plan(TENANT, ACCOUNT, FAN, 1)
            assert decisions(database) == [GOOD] and len(client.requests) == 1 and len(rows(database)) == 1
    finally:
        store.close()


@pytest.mark.parametrize('interrupt', [False, True])
def test_length_limited_gets_at_most_the_existing_one_regeneration(database, interrupt):
    store, authority, engine, runtime, client, _ = with_model(database, [answer(CUT, 'length'), answer(CUT, 'length'),
                                                                          answer(GOOD)])
    try:
        incoming(store, 1, 'hello')
        if interrupt:
            crash_at(runtime, 'after_settle')
            with pytest.raises(RuntimeError):
                engine.plan(TENANT, ACCOUNT, FAN, 1)
            restart(database, engine, client, config())
        with pytest.raises(Refused, match='could not write an acceptable reply'):
            engine.plan(TENANT, ACCOUNT, FAN, 1)
        assert len(client.requests) == 2 and decisions(database) == []
        assert [r.outcome for r in rows(database)] == ['invalid', 'invalid']
        with pytest.raises(Refused):
            engine.plan(TENANT, ACCOUNT, FAN, 1)
        assert len(client.requests) == 2  # a later restart cannot buy a third attempt
    finally:
        store.close()


@pytest.mark.parametrize('pinned', [False, True])
def test_genuine_refusal_with_length_finish_is_not_retried_even_after_restart(database, pinned):
    cfg = config(**(PINNED if pinned else {}))
    refusal = answer('I cannot help with that request.', 'length', pinned=pinned)
    store, authority, engine, runtime, client, _ = with_model(database, [refusal, answer(GOOD, pinned=pinned)], cfg=cfg)
    try:
        incoming(store, 1, 'hello')
        crash_at(runtime, 'before_set_outcome' if not pinned else 'after_finish_identity')
        with pytest.raises(RuntimeError):
            engine.plan(TENANT, ACCOUNT, FAN, 1)
        restart(database, engine, client, cfg)
        with pytest.raises(Refused, match='could not write an acceptable reply'):
            engine.plan(TENANT, ACCOUNT, FAN, 1)
        assert len(client.requests) == 1 and decisions(database) == []
        assert [r.outcome for r in rows(database)] == ['refusal']
    finally:
        store.close()


def test_unknown_cost_length_limited_keeps_its_reservation_and_cannot_become_complete(database):
    unknown = answer(CUT, 'length', usage=Usage(None, None, None))
    store, authority, engine, runtime, client, _ = with_model(database, [unknown, answer(GOOD, cost=700)])
    try:
        incoming(store, 1, 'hello')
        crash_at(runtime, 'after_settle')
        with pytest.raises(RuntimeError):
            engine.plan(TENANT, ACCOUNT, FAN, 1)
        [first] = rows(database)
        assert (first.status, first.usage_known, first.actual_microusd, first.outcome) == ('unknown', False, None, LENGTH_LIMITED)
        day = first.cost_day
        assert runtime.ledger.committed(day=day) == first.reserved_microusd
        with pytest.raises(Refused, match='can never be recorded as complete'):
            runtime.ledger.set_outcome(ACCOUNT, first.call_id, 'ok')
        with pytest.raises(Refused, match='can never be recorded as complete'):
            runtime.ledger.set_outcome(ACCOUNT, first.call_id, 'verified_ok')
        assert rows(database)[0].outcome == LENGTH_LIMITED
        restart(database, engine, client, config())
        assert engine.plan(TENANT, ACCOUNT, FAN, 1)
        saved = rows(database)
        assert len(saved) == 2 and saved[0].status == 'unknown' and saved[0].actual_microusd is None
        # the unknown call is never refunded: its full reservation stays counted next to the second call's actual cost
        assert runtime.ledger.committed(day=day) == saved[0].reserved_microusd + 700
        assert decisions(database) == [GOOD]
    finally:
        store.close()


def test_unreconciled_length_pending_stays_visible_as_identity_unverified_work(database):
    cfg = config(**PINNED)
    store, authority, engine, runtime, client, _ = with_model(database, [answer(CUT, 'length', pinned=True)], cfg=cfg)
    try:
        incoming(store, 1, 'hello')
        crash_at(runtime, 'after_settle')
        with pytest.raises(RuntimeError):
            engine.plan(TENANT, ACCOUNT, FAN, 1)
        from conversation_engine.model_ledger import IDENTITY_UNSETTLED
        assert rows(database)[0].outcome in IDENTITY_UNSETTLED
        with Session(database[1]) as s:
            assert s.scalar(select(func.count()).select_from(OfEngineModelCall).where(
                OfEngineModelCall.outcome.in_(IDENTITY_UNSETTLED))) == 1
    finally:
        store.close()


def test_concurrent_workers_replay_one_regeneration_and_settle_once(database):
    store, authority, engine, runtime, client, _ = with_model(database, [answer(CUT, 'length')])
    try:
        incoming(store, 1, 'hello')
        crash_at(runtime, 'after_settle')
        with pytest.raises(RuntimeError):
            engine.plan(TENANT, ACCOUNT, FAN, 1)
        first = rows(database)[0]
        clients = [FixtureChatClient([answer(GOOD)], cost_microusd=500) for _ in range(2)]
        engines = []
        for c in clients:
            rt = ModelRuntime(database[1], c, config(), StaticCreatorProfiles({ACCOUNT: PROFILE}))
            engines.append(EngineService(store, rt))
        barrier, outcomes = threading.Barrier(2), {}

        def work(i):
            barrier.wait()
            try:
                outcomes[i] = engines[i].plan(TENANT, ACCOUNT, FAN, 1)
            except Exception as exc:  # the loser may be told the call is busy or already done
                outcomes[i] = exc
        threads = [threading.Thread(target=work, args=(i,)) for i in range(2)]
        [t.start() for t in threads]
        [t.join(60) for t in threads]
        calls = sum(len(c.requests) for c in clients)
        saved = rows(database)
        assert calls == 1, (calls, outcomes)
        assert len(saved) == 2 and [r.outcome for r in saved] == ['invalid', 'ok']
        assert saved[1].status == 'settled' and saved[1].actual_microusd == 500
        assert runtime.ledger.committed(day=first.cost_day) == 500 + 500  # first call actual + one regeneration, nothing doubled
        assert any(isinstance(v, str) for v in outcomes.values())
        assert all(isinstance(v, (str, Exception)) for v in outcomes.values())
    finally:
        store.close()


def _child_dies_after_settle(database, engine, runtime):
    database[1].dispose(close=False)
    original = runtime.ledger.settle

    def settle_then_die(*a, **kw):
        original(*a, **kw)
        os._exit(137)  # SIGKILL-like: no exception handling, no cleanup, connection dropped
    runtime.ledger.settle = settle_then_die
    engine.plan(TENANT, ACCOUNT, FAN, 1)
    os._exit(0)


@pytest.mark.parametrize('pinned', [False, True])
def test_process_killed_after_response_persisted_keeps_length_verdict(database, pinned):
    cfg = config(**(PINNED if pinned else {}))
    store, authority, engine, runtime, client, _ = with_model(database, [answer(CUT, 'length', pinned=pinned),
                                                                          answer(GOOD, pinned=pinned)], cfg=cfg)
    try:
        incoming(store, 1, 'hello')
        ctx = multiprocessing.get_context('fork')
        child = ctx.Process(target=_child_dies_after_settle, args=(database, engine, runtime))
        child.start(); child.join(120)
        assert child.exitcode == 137
        database[1].dispose(close=False)
        [saved] = rows(database)
        assert saved.outcome == (LENGTH_PENDING if pinned else LENGTH_LIMITED) and saved.response_text is not None
        # the child consumed its own copy of the provider script; the restarted worker gets a fresh provider client
        runtime2 = restart(database, engine, FixtureChatClient([answer(GOOD, pinned=pinned)], cost_microusd=500), cfg)
        runtime2.worker_id = 'restarted-worker'
        if pinned:
            with pytest.raises(Refused, match='identity is unverified'):
                engine.plan(TENANT, ACCOUNT, FAN, 1)
            assert decisions(database) == []
        else:
            assert engine.plan(TENANT, ACCOUNT, FAN, 1)
            assert decisions(database) == [GOOD]
    finally:
        store.close()
