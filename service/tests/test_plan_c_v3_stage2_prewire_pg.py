"""Stage 2C: an attempt that provably never reached the wire is retired, not held unknown; a wired one stays unknown."""
import time

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from db.models import OfConversationState
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_engine_models import OfEngineDecision, OfEngineRequest
from fake_fans_server import FakeFans
from model_transport.pilot_supervised import build_supervised_pilot
from onlyfans_runtime.store import Refused
from test_engine_real_worker_pg import world  # noqa: F401
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT, accepted
from test_plan_c_draft_app_pg import configured_draft  # noqa: F401
from test_plan_c_supervised_pg import approve, stage, supervised  # noqa: F401
from test_real_transport_pg import build, real_flag, status, text_intent  # noqa: F401

SCOPE = frozenset({FAN})


def sql(database, statement, **params):
    with database[1].begin() as c:
        c.execute(text(statement), params)


def expire_deadline(database, intent):
    sql(database, "UPDATE of_text_intent SET deadline = clock_timestamp() - interval '1 second' WHERE intent_id=:i", i=intent)


def facts(database, intent):
    with Session(database[1]) as s:
        row = s.get(OfTextIntent, (ACCOUNT, intent))
        policy = s.get(OfDeliveryPolicy, ACCOUNT)
        decision = s.scalar(select(OfEngineDecision).where(OfEngineDecision.intent_id == intent))
        request = s.get(OfEngineRequest, (ACCOUNT, decision.request_id))
        state = s.get(OfConversationState, (ACCOUNT, FAN))
        return dict(status=row.status, reason=row.reason, token=row.dispatch_token, wired=row.wire_started_at is not None,
                    held=row.reservation_held, used=policy.used_units, queued=policy.queued_count,
                    decision=decision.status, request=request.status, unresolved=state.unresolved_attempt_id)


def test_never_wired_crash_is_retired_with_only_the_unit_released(database):
    fake = FakeFans()
    try:
        store, authority, engine, transport, _ = build(database, fake)
        intent = text_intent(engine, store)
        grant = authority.admit(TENANT, ACCOUNT, intent)
        before = facts(database, intent)
        assert before['status'] == 'dispatching' and before['held'] and before['used'] == 1 and not before['wired']
        expire_deadline(database, intent)
        assert authority.expire_dispatches(TENANT, ACCOUNT, synthetic_engine_only=True, allowed_fans=SCOPE) == 1
        after = facts(database, intent)
        assert after['status'] == 'cancelled' and after['reason'] == 'prewire_crash_never_wired'
        assert after['token'] is None and not after['wired'] and not after['held']
        assert after['used'] == 0 and after['queued'] == 0, 'exactly the held delivery unit is released'
        assert after['decision'] == 'cancelled' and after['request'] == 'open' and after['unresolved'] is None
        assert fake.posts == []
        # The old token is dead for every writer, including a late worker that still holds the grant.
        with pytest.raises(Refused):
            authority.consume_dispatch(TENANT, grant)
        with pytest.raises(Refused):
            authority.record_accepted(TENANT, ACCOUNT, intent, grant.token, accepted(grant))
        with pytest.raises(Refused):
            authority.mark_unknown(TENANT, ACCOUNT, intent, grant.token)
        with pytest.raises(Refused):
            transport.send(grant, authority=authority, tenant_id=TENANT)
        assert fake.posts == [] and facts(database, intent) == after
        # Repeating recovery changes nothing and releases nothing twice.
        assert authority.expire_dispatches(TENANT, ACCOUNT, synthetic_engine_only=True, allowed_fans=SCOPE) == 0
        assert facts(database, intent) == after
    finally:
        fake.close()


def test_worker_racing_to_begin_the_wire_after_the_deadline_cannot_send(database, monkeypatch):
    fake = FakeFans()
    try:
        store, authority, engine, transport, _ = build(database, fake)
        intent = text_intent(engine, store)
        real_scope = transport._scope
        monkeypatch.setattr('onlyfans_runtime.text_delivery.DISPATCH_SECONDS', 1)

        def slow_worker(dispatch):
            real_scope(dispatch)
            time.sleep(1.4)    # the real deadline passes just before the wire marker commit
        monkeypatch.setattr(transport, '_scope', slow_worker)
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'cancelled'
        after = facts(database, intent)
        assert fake.posts == [] and not after['wired'] and after['token'] is None
        assert after['status'] == 'cancelled' and after['reason'] == 'prewire_dispatch_expired'
        assert after['used'] == 0 and not after['held'] and after['unresolved'] is None
    finally:
        fake.close()


def test_wire_started_before_the_deadline_stays_unknown_and_keeps_its_reservation(database):
    fake = FakeFans()
    try:
        store, authority, engine, transport, _ = build(database, fake)
        intent = text_intent(engine, store)
        grant = authority.admit(TENANT, ACCOUNT, intent)
        assert authority.consume_dispatch(TENANT, grant) == grant      # the wire marker is committed ...
        expire_deadline(database, intent)                              # ... and the process then dies
        assert authority.expire_dispatches(TENANT, ACCOUNT, synthetic_engine_only=True, allowed_fans=SCOPE) == 1
        after = facts(database, intent)
        assert after['status'] == 'unknown' and after['reason'] == 'dispatch_expired' and after['wired']
        assert after['held'] and after['used'] == 1 and after['decision'] == 'unknown'
        assert after['unresolved'] is not None and after['token'] == grant.token
    finally:
        fake.close()


@pytest.mark.parametrize('case', ['legacy_wire_version', 'not_engine_scoped'])
def test_legacy_or_ambiguous_attempts_stay_unknown(database, case):
    fake = FakeFans()
    try:
        store, authority, engine, transport, _ = build(database, fake)
        intent = text_intent(engine, store)
        authority.admit(TENANT, ACCOUNT, intent)
        if case == 'legacy_wire_version':
            sql(database, "UPDATE of_text_intent SET wire_version = 0 WHERE intent_id=:i", i=intent)
        expire_deadline(database, intent)
        kwargs = dict(synthetic_engine_only=True, allowed_fans=SCOPE) if case == 'legacy_wire_version' else {}
        assert authority.expire_dispatches(TENANT, ACCOUNT, **kwargs) == 1
        after = facts(database, intent)
        assert after['status'] == 'unknown' and after['held'] and after['used'] == 1
        assert after['token'] is not None and after['unresolved'] is not None
    finally:
        fake.close()


class Crash(BaseException):
    """Simulates the process dying; it bypasses every `except Exception` like a real kill would."""


def test_pilot_restart_retires_a_never_wired_attempt_without_post_or_unknown(supervised, monkeypatch):
    world, env, model, seen, pilot = supervised
    result = stage(pilot, 99301)
    approve(world, result)

    def die(*args, **kwargs):
        raise Crash()
    monkeypatch.setattr(pilot.worker.transport, 'send', die)
    with pytest.raises(Crash):
        pilot.worker.send_approved(TENANT, ACCOUNT, result['decisionId'])
    with Session(world.database[1]) as s:
        intent = s.scalar(select(OfTextIntent))
        assert intent.status == 'dispatching' and intent.wire_started_at is None
        intent_id = intent.intent_id
    sql(world.database, "UPDATE of_text_intent SET deadline = clock_timestamp() - interval '1 second'")
    restarted = build_supervised_pilot(world.store, env, model_transport=model, fans_transport=world.fans.transport)
    for _ in range(3):
        restarted.tick()
    assert world.fans.posts == []
    after = facts(world.database, intent_id)
    assert after['status'] == 'cancelled' and after['reason'] == 'prewire_crash_never_wired'
    assert after['used'] == 0 and after['unresolved'] is None and after['decision'] == 'cancelled'
