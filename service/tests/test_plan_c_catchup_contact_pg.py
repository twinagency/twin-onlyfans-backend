"""Recovered pilot contact stops share the normal durable send barrier."""
import pytest
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from conversation_engine import approval, pilot_scope
from conversation_engine.pilot_ingress import preserve_contact_stops
from db.models import OfConversationState
from db.onlyfans_models import OfControlAudit
from onlyfans_runtime.store import Refused
from onlyfans_runtime.text_delivery import TextAuthority
from test_plan_c_supervised_pg import supervised, stage
from test_plan_c_draft_app_pg import configured_draft
from test_engine_real_worker_pg import world, inbound, FAN2
from test_onlyfans_foundation_pg import database
from test_onlyfans_text_delivery_pg import TENANT, ACCOUNT, FAN


def recover(pilot, mid, body, fan=FAN):
    return pilot.worker.intake.ingest_history_item(
        TENANT, ACCOUNT, fan, inbound(mid, body, fan=fan)['payload'])


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_recovered_stop_then_greeting_blocks_model_and_send(supervised):
    w, _, _, seen, pilot = supervised
    stage(pilot, 2600, 'hello')
    before = len(seen)
    assert recover(pilot, 2601, 'stop messaging me') == 'accepted'
    assert recover(pilot, 2602, 'hello again') == 'accepted'
    w.store.drain(TENANT, ACCOUNT)
    preserve_contact_stops(w.store, TENANT, ACCOUNT)
    try:
        result = pilot.worker.turn(TENANT, ACCOUNT, FAN, 2602)
        assert result.get('outcome') in ('not_ready', 'refused', 'held'), result
    except Refused:
        pass
    with Session(w.database[1]) as s:
        assert s.get(OfConversationState, (ACCOUNT, FAN)).mode == 'no_contact'
    assert len(seen) == before
    assert w.fans.posts == []


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
@pytest.mark.parametrize('drain', [False, True], ids=['pending', 'applied'])
def test_recovered_stop_after_dispatch_grant_blocks_wire(supervised, monkeypatch, drain):
    w, _, _, _, pilot = supervised
    staged = stage(pilot, 2700, 'hello')
    info = staged['approval']
    approval.authorize_automatic(w.store, TENANT, ACCOUNT, info['requestId'], info['generation'],
                                 expected_version=info['version'])
    original = TextAuthority.consume_dispatch
    reached, reasons = [], []
    def interleaved(self, tenant, grant, **kw):
        if not reached:
            reached.append(True)
            assert recover(pilot, 2701, 'stop messaging me') == 'accepted'
            assert recover(pilot, 2702, 'hello again') == 'accepted'
            if drain:
                w.store.drain(TENANT, ACCOUNT)
            with Session(w.database[1]) as s, s.begin():
                runtime = w.store._runtime(s, TENANT, ACCOUNT)
                reasons.append(pilot_scope.delivery_block(s, runtime, FAN))
        return original(self, tenant, grant, **kw)
    monkeypatch.setattr(TextAuthority, 'consume_dispatch', interleaved)
    try:
        pilot.worker.send_approved(TENANT, ACCOUNT, staged['decisionId'])
    except Refused:
        pass
    assert reached
    assert reasons == ['pilot_contact_review_pending']
    assert w.fans.posts == []


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_duplicate_reordered_catchup_stop_survives_restart(supervised):
    from model_transport.pilot_supervised import build_supervised_pilot
    w, env, model, seen, pilot = supervised
    stage(pilot, 2800, 'hello')
    assert recover(pilot, 2802, 'hello again') == 'accepted'
    stop = inbound(2801, 'stop messaging me')['payload']
    assert pilot.worker.intake.ingest_history_item(TENANT, ACCOUNT, FAN, stop) == 'accepted'
    assert pilot.worker.intake.ingest_history_item(TENANT, ACCOUNT, FAN, stop) == 'duplicate'
    w.store.drain(TENANT, ACCOUNT)
    preserve_contact_stops(w.store, TENANT, ACCOUNT)
    preserve_contact_stops(w.store, TENANT, ACCOUNT)
    with Session(w.database[1]) as s:
        assert s.get(OfConversationState, (ACCOUNT, FAN)).mode == 'no_contact'
        count = s.scalar(select(func.count()).select_from(OfControlAudit).where(
            OfControlAudit.account_id == ACCOUNT, OfControlAudit.fan_id == FAN,
            OfControlAudit.action == 'mark_no_contact'))
    assert count == 1
    restarted = build_supervised_pilot(w.store, env, model_transport=model, fans_transport=w.fans.transport)
    before = len(seen)
    restarted.tick()
    assert len(seen) == before and w.fans.posts == []
    with Session(w.database[1]) as s:
        assert s.get(OfConversationState, (ACCOUNT, FAN)).mode == 'no_contact'
        assert s.scalar(select(func.count()).select_from(OfControlAudit).where(
            OfControlAudit.account_id == ACCOUNT, OfControlAudit.fan_id == FAN,
            OfControlAudit.action == 'mark_no_contact')) == 1


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_other_fan_pending_stop_does_not_block_nonstop_catchup(supervised):
    w, _, _, seen, pilot = supervised
    stage(pilot, 2900, 'hello', fan=FAN)
    assert recover(pilot, 2901, 'stop messaging me', fan=FAN2) == 'accepted'
    with Session(w.database[1]) as s, s.begin():
        runtime = w.store._runtime(s, TENANT, ACCOUNT)
        assert pilot_scope.delivery_block(s, runtime, FAN2) == 'pilot_contact_review_pending'
        assert pilot_scope.delivery_block(s, runtime, FAN) is None
    assert recover(pilot, 2902, 'hello again', fan=FAN) == 'accepted'
    w.store.drain(TENANT, ACCOUNT)
    preserve_contact_stops(w.store, TENANT, ACCOUNT)
    with Session(w.database[1]) as s:
        assert s.get(OfConversationState, (ACCOUNT, FAN2)).mode == 'no_contact'
        assert s.get(OfConversationState, (ACCOUNT, FAN)).mode == 'automated'
    before = len(seen)
    result = pilot.worker.turn(TENANT, ACCOUNT, FAN, 2902)
    assert result.get('outcome') == 'awaiting_approval', result
    assert len(seen) == before + 1


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_history_before_scope_and_creator_import_do_not_trigger_stop(supervised):
    from datetime import timedelta, timezone
    from onlyfans_runtime.store import Event
    w, _, _, _, pilot = supervised
    stage(pilot, 3000, 'hello')
    with Session(w.database[1]) as s:
        runtime = w.store._runtime(s, TENANT, ACCOUNT, lock=False)
        scope, _ = pilot_scope.require(s, runtime)
        old = (scope.created_at - timedelta(seconds=2)).replace(tzinfo=timezone.utc).isoformat()
    assert w.store.record(TENANT, ACCOUNT,
        Event(FAN, 3001, old, 'in', 'stop messaging me'), source='catchup') == 'accepted'
    assert w.store.record(TENANT, ACCOUNT,
        Event(FAN, 3002, old, 'out', 'stop messaging me'), source='import') == 'accepted'
    w.store.drain(TENANT, ACCOUNT)
    preserve_contact_stops(w.store, TENANT, ACCOUNT)
    with Session(w.database[1]) as s, s.begin():
        runtime = w.store._runtime(s, TENANT, ACCOUNT)
        assert s.get(OfConversationState, (ACCOUNT, FAN)).mode == 'automated'
        assert pilot_scope.delivery_block(s, runtime, FAN) is None


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_malformed_pending_catchup_holds_only_the_affected_fan(supervised):
    from db.onlyfans_models import OfIngress
    from onlyfans_runtime.store import utcnow
    w, _, _, _, pilot = supervised
    stage(pilot, 3100, 'hello')
    with Session(w.database[1]) as s, s.begin():
        runtime = w.store._runtime(s, TENANT, ACCOUNT)
        s.add(OfIngress(account_id=ACCOUNT, event_key='e'*64, binding_id=runtime.binding_id,
            fan_id=FAN, message_id=3101, source='catchup', payload='{bad-json',
            status='pending', received_at=utcnow()))
    with Session(w.database[1]) as s, s.begin():
        runtime = w.store._runtime(s, TENANT, ACCOUNT)
        assert pilot_scope.delivery_block(s, runtime, FAN) == 'pilot_contact_review_pending'
        assert pilot_scope.delivery_block(s, runtime, FAN2) is None
    assert w.fans.posts == []


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_recovered_stop_in_another_account_cannot_control_pilot(supervised):
    from test_onlyfans_foundation_pg import seed, event
    w, _, _, _, pilot = supervised
    stage(pilot, 3200, 'hello')
    other = seed(w.database[0], w.database[1], account='other-account',
                 tenant='other-tenant', fan=FAN)
    try:
        assert other.record('other-tenant', 'other-account',
                            event(mid=3201, fan=FAN, body='stop messaging me'),
                            source='catchup') == 'accepted'
        other.drain('other-tenant', 'other-account')
        preserve_contact_stops(w.store, TENANT, ACCOUNT)
        with Session(w.database[1]) as s, s.begin():
            runtime = w.store._runtime(s, TENANT, ACCOUNT)
            assert s.get(OfConversationState, (ACCOUNT, FAN)).mode == 'automated'
            assert pilot_scope.delivery_block(s, runtime, FAN) is None
        assert w.fans.posts == []
    finally:
        other.close()
