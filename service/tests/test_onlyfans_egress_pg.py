"""One-use egress acceptance on disposable PostgreSQL; no provider network."""
from dataclasses import replace
from datetime import timedelta, timezone
import multiprocessing
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, event
from test_onlyfans_text_delivery_pg import (unthrottle,
    ACCOUNT, FAN, TENANT, accepted, assert_no_outbound, configured, prepare,
)
from db.models import Fan, Message, OfConversationState, OfCreatorBinding, OfMessageDelivery
from db.onlyfans_models import OfIngress, OfRuntime
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from onlyfans_runtime.store import Refused, Store, utcnow
from onlyfans_runtime.text_delivery import Dispatch, SyntheticTextTransport, TextAuthority


def test_direct_transport_consumes_once_and_copied_grant_cannot_repeat(database):
    _, engine = database
    store, authority = configured(database)
    intent = prepare(authority)
    grant = authority.admit(TENANT, ACCOUNT, intent)
    assert grant.wire_version == 1 and grant.policy_revision == 1
    transport = SyntheticTextTransport(ACCOUNT)
    proof = transport.send(grant, authority=authority, tenant_id=TENANT)
    for copied in (grant, replace(grant)):
        with pytest.raises(Refused):
            transport.send(copied, authority=authority, tenant_id=TENANT)
    assert len(transport.calls) == 1
    assert authority.record_accepted(TENANT, ACCOUNT, intent, grant.token, proof) == "accepted"
    authority.materialize(TENANT, ACCOUNT, intent)
    with Session(engine) as s:
        row = s.get(OfTextIntent, (ACCOUNT, intent))
        assert row.wire_started_at is not None and row.projected_at is not None
        assert s.get(OfDeliveryPolicy, ACCOUNT).used_units == 1
    store.close()


def test_tampered_grants_refuse_without_mutating_or_consuming_the_valid_grant(database):
    _, engine = database
    store, authority = configured(database)
    intent = prepare(authority)
    grant = authority.admit(TENANT, ACCOUNT, intent)
    changes = [dict(account_id="other-account"), dict(fan_id=102), dict(fan_id=True),
               dict(intent_id=str(uuid.uuid4())), dict(attempt_id=str(uuid.uuid4())),
               dict(token=str(uuid.uuid4())), dict(body="Changed text"), dict(payload_hash="0"*64),
               dict(origin="automated"), dict(deadline=grant.deadline+timedelta(seconds=1)),
               dict(deadline=grant.deadline.replace(tzinfo=timezone.utc)),
               dict(policy_revision=2), dict(policy_revision=True),
               dict(wire_version=0), dict(wire_version=True)]
    transport = SyntheticTextTransport(ACCOUNT)
    with pytest.raises(Refused):
        transport.send(grant, authority=SimpleNamespace(consume_dispatch=lambda *args: grant), tenant_id=TENANT)
    class DerivedDispatch(Dispatch):
        pass
    with pytest.raises(Refused):
        transport.send(DerivedDispatch(**grant.__dict__), authority=authority, tenant_id=TENANT)
    for changed in changes:
        with pytest.raises(Refused):
            transport.send(replace(grant, **changed), authority=authority, tenant_id=TENANT)
        with Session(engine) as s:
            row = s.get(OfTextIntent, (ACCOUNT, intent))
            assert row.status == "dispatching" and row.wire_started_at is None
            assert row.reservation_held and s.get(OfDeliveryPolicy, ACCOUNT).used_units == 1
    with pytest.raises(Refused):
        transport.send(grant, authority=authority, tenant_id="other-tenant")
    with pytest.raises(Refused):
        authority.consume_dispatch(TENANT, grant, transport_generation=True)
    assert not transport.calls
    transport.send(grant, authority=authority, tenant_id=TENANT)
    assert len(transport.calls) == 1
    store.close()


@pytest.mark.parametrize("change", ["paused", "no_contact", "takeover", "input", "pending", "recovery",
                                   "quarantine", "policy_rotation", "cap_decrease", "revoke", "disabled",
                                   "muted", "retained_hold", "other_pointer"])
def test_prewire_changes_cancel_once_refund_once_and_make_no_call(database, change):
    _, engine = database
    store, authority = configured(database)
    intent = prepare(authority)
    grant = authority.admit(TENANT, ACCOUNT, intent)
    if change in {"paused", "no_contact"}:
        store.control(TENANT, ACCOUNT, FAN, mode=change, expected_revision=0,
                      command_key="prewire-stop", actor="synthetic-operator", reason="Stop before network")
    elif change == "input":
        store.record(TENANT, ACCOUNT, event())
        store.drain(TENANT, ACCOUNT)
    elif change == "policy_rotation":
        authority.configure(TENANT, ACCOUNT, expected_revision=1, enabled=True,
                            limit_units=20, throttle_seconds=1, actor="synthetic-operator", reason="Rotate policy before wire")
    else:
        with Session(engine) as s, s.begin():
            state = s.get(OfConversationState, (ACCOUNT, FAN))
            runtime = s.get(OfRuntime, ACCOUNT)
            if change == "takeover": state.takeover_state = "requested"
            elif change == "pending": runtime.pending_events = 1
            elif change == "recovery": runtime.recovery_needed = True
            elif change == "disabled": runtime.enabled = False
            elif change == "muted": s.get(Fan, (ACCOUNT, FAN)).is_muted = True
            elif change == "other_pointer": state.unresolved_attempt_id = "other-held-attempt"
            elif change == "cap_decrease": s.get(OfDeliveryPolicy, ACCOUNT).limit_units = 0
            elif change == "revoke": s.get(OfCreatorBinding, "binding-"+ACCOUNT).revoked_at = utcnow()
            elif change == "quarantine":
                s.add(OfIngress(account_id=ACCOUNT, binding_id="binding-"+ACCOUNT, event_key="a"*64,
                                fan_id=FAN, message_id=7, source="rest", payload="{}", status="quarantined", received_at=utcnow()))
            elif change == "retained_hold":
                s.add(Message(account_id=ACCOUNT, fan_id=FAN, message_id=7, direction="out", body="Retained ambiguity", created_at=utcnow()))
                s.flush()
                s.add(OfMessageDelivery(account_id=ACCOUNT, fan_id=FAN, message_id=7, outcome="unknown", evidence_ref="retained"))
    transport = SyntheticTextTransport(ACCOUNT)
    with pytest.raises(Refused):
        transport.send(grant, authority=authority, tenant_id=TENANT)
    with pytest.raises(Refused):
        transport.send(replace(grant), authority=authority, tenant_id=TENANT)
    assert not transport.calls
    with Session(engine) as s:
        row = s.get(OfTextIntent, (ACCOUNT, intent))
        policy = s.get(OfDeliveryPolicy, ACCOUNT)
        if change == "pending":
            # Fresh ingress is a bounded deferral before any wire, not a
            # permanent cancellation; the old one-use grant remains spent.
            assert row.status == "queued" and row.reason == "history_not_current"
            assert (policy.used_units, policy.queued_count, policy.active_intent_id) == (0, 1, None)
        else:
            assert row.status in {"cancelled", "refused"} and row.reason.startswith("prewire_")
            assert (policy.used_units, policy.queued_count, policy.active_intent_id) == (0, 0, None)
        assert row.wire_started_at is None and not row.reservation_held
        pointer = s.get(OfConversationState, (ACCOUNT, FAN)).unresolved_attempt_id
        assert pointer == ("other-held-attempt" if change == "other_pointer" else None)
    if change == "pending":
        with Session(engine) as s, s.begin():s.get(OfRuntime, ACCOUNT).pending_events = 0
        unthrottle(engine)
        fresh = authority.admit(TENANT, ACCOUNT, intent)
        assert fresh.attempt_id != grant.attempt_id and fresh.token != grant.token
        with pytest.raises(Refused):transport.send(grant, authority=authority, tenant_id=TENANT)
        proof = transport.send(fresh, authority=authority, tenant_id=TENANT)
        assert authority.record_accepted(TENANT, ACCOUNT, intent, fresh.token, proof) == "accepted"
        assert authority.materialize(TENANT, ACCOUNT, intent)
        assert len(transport.calls) == 1
    store.close()


@pytest.mark.parametrize("changed", ["deadline", "intent_ttl", "legacy"])
def test_expired_or_legacy_unconsumed_attempt_stays_unknown_and_charged(database, changed):
    _, engine = database
    store, authority = configured(database)
    intent = prepare(authority)
    original = authority.admit(TENANT, ACCOUNT, intent)
    with Session(engine) as s, s.begin():
        row = s.get(OfTextIntent, (ACCOUNT, intent))
        if changed == "deadline": row.deadline = utcnow()-timedelta(seconds=1)
        elif changed == "intent_ttl": row.expires_at = utcnow()-timedelta(seconds=1)
        else: row.wire_version = 0
        grant = authority._grant(row)
    transport = SyntheticTextTransport(ACCOUNT)
    with pytest.raises(Refused):
        transport.send(grant, authority=authority, tenant_id=TENANT)
    assert not transport.calls
    with Session(engine) as s:
        row = s.get(OfTextIntent, (ACCOUNT, intent))
        assert row.status == "unknown" and row.wire_started_at is None and row.reservation_held
        assert s.get(OfDeliveryPolicy, ACCOUNT).used_units == 1
        assert s.get(OfDeliveryPolicy, ACCOUNT).active_intent_id == intent
    # Historical proof remains recordable even when an old process did not
    # persist the new network marker. Repair must not invent another send.
    assert authority.record_accepted(TENANT, ACCOUNT, intent, original.token, accepted(original)) == "accepted"
    assert authority.repair(TENANT, ACCOUNT) == 1
    assert not transport.calls
    store.close()


def test_transport_generation_mismatch_is_checked_before_reserving_a_unit(database):
    _, engine = database
    store, authority = configured(database)
    intent = prepare(authority)
    assert authority.admit(TENANT, ACCOUNT, intent, transport_generation=2) == "cancelled"
    with Session(engine) as s:
        assert s.get(OfDeliveryPolicy, ACCOUNT).used_units == 0
        assert not s.get(OfTextIntent, (ACCOUNT, intent)).reservation_held
    store.close()


def test_transport_rotation_after_admission_cancels_without_wire(database):
    _, engine = database
    store, authority = configured(database)
    intent = prepare(authority)
    grant = authority.admit(TENANT, ACCOUNT, intent, transport_generation=1)
    assert authority.consume_dispatch(TENANT, grant, transport_generation=2) is None
    with Session(engine) as s:
        assert s.get(OfTextIntent, (ACCOUNT, intent)).wire_started_at is None
        assert s.get(OfDeliveryPolicy, ACCOUNT).used_units == 0
    store.close()


def test_legacy_queued_and_dispatching_rows_are_never_resubmitted(database):
    _, engine = database
    store, authority = configured(database)
    queued = prepare(authority, "legacy-queued")
    with Session(engine) as s, s.begin(): s.get(OfTextIntent, (ACCOUNT, queued)).wire_version = 0
    transport = SyntheticTextTransport(ACCOUNT)
    assert authority.dispatch(TENANT, ACCOUNT, queued, transport) == "cancelled"
    active = prepare(authority, "legacy-dispatching")
    authority.admit(TENANT, ACCOUNT, active)
    with Session(engine) as s, s.begin(): s.get(OfTextIntent, (ACCOUNT, active)).wire_version = 0
    assert authority.dispatch(TENANT, ACCOUNT, active, transport) == "unknown"
    assert not transport.calls
    with Session(engine) as s:
        assert s.get(OfTextIntent, (ACCOUNT, queued)).wire_started_at is None
        assert not s.get(OfTextIntent, (ACCOUNT, queued)).reservation_held
        assert s.get(OfTextIntent, (ACCOUNT, active)).reservation_held
        assert s.get(OfDeliveryPolicy, ACCOUNT).used_units == 1
    store.close()


def _consume_process(url, grant, ready, go, output):
    store = Store(url)
    try:
        authority = TextAuthority(store)
        transport = SyntheticTextTransport(ACCOUNT)
        ready.put(True)
        if not go.wait(15): raise AssertionError("synthetic consume barrier timed out")
        try:
            transport.send(grant, authority=authority, tenant_id=TENANT)
        except Refused:
            output.put(("refused", len(transport.calls)))
        else:
            output.put(("sent", len(transport.calls)))
    finally:
        store.close()


def test_two_processes_consume_one_capability_once(database):
    url, engine = database
    store, authority = configured(database)
    intent = prepare(authority)
    grant = authority.admit(TENANT, ACCOUNT, intent)
    ctx = multiprocessing.get_context("spawn")
    ready, output, go = ctx.Queue(), ctx.Queue(), ctx.Event()
    workers = [ctx.Process(target=_consume_process, args=(url, grant, ready, go, output)) for _ in range(2)]
    for worker in workers: worker.start()
    try:
        for _ in workers: assert ready.get(timeout=20)
        go.set()
        results = [output.get(timeout=20) for _ in workers]
        for worker in workers: worker.join(15); assert worker.exitcode == 0
        assert sorted(results) == [("refused", 0), ("sent", 1)]
        with Session(engine) as s:
            assert s.get(OfTextIntent, (ACCOUNT, intent)).wire_started_at is not None
            assert s.get(OfDeliveryPolicy, ACCOUNT).used_units == 1
    finally:
        for worker in workers:
            if worker.is_alive(): worker.terminate(); worker.join(5)
        store.close()


def test_stop_or_revoke_after_consumption_retains_accepted_evidence(database):
    _, engine = database
    store, authority = configured(database)
    intent = prepare(authority)
    grant = authority.admit(TENANT, ACCOUNT, intent)
    assert authority.consume_dispatch(TENANT, grant) == grant
    store.control(TENANT, ACCOUNT, FAN, mode="no_contact", expected_revision=0,
                  command_key="stop-after-start", actor="synthetic-operator", reason="Stop after network marker")
    with Session(engine) as s, s.begin(): s.get(OfCreatorBinding, "binding-"+ACCOUNT).revoked_at = utcnow()
    assert authority.record_accepted(TENANT, ACCOUNT, intent, grant.token, accepted(grant)) == "accepted"
    assert authority.materialize(TENANT, ACCOUNT, intent)
    with Session(engine) as s:
        assert s.get(OfConversationState, (ACCOUNT, FAN)).mode == "no_contact"
        assert s.get(Message, (ACCOUNT, FAN, 9001)).body == "Synthetic reply"
        assert s.get(OfTextIntent, (ACCOUNT, intent)).wire_started_at is not None
    store.close()


def test_delayed_handoff_and_receipt_cannot_spend_the_next_wire_cooldown(database):
    _, engine = database
    store, authority = configured(database, throttle=60)
    intent = prepare(authority)
    grant = authority.admit(TENANT, ACCOUNT, intent)
    with engine.begin() as c: c.exec_driver_sql("UPDATE of_delivery_policy SET next_dispatch_at=clock_timestamp()-INTERVAL '1 second'")
    transport = SyntheticTextTransport(ACCOUNT)
    proof = transport.send(grant, authority=authority, tenant_id=TENANT)
    with Session(engine) as s:
        row = s.get(OfTextIntent, (ACCOUNT, intent))
        assert s.get(OfDeliveryPolicy, ACCOUNT).next_dispatch_at >= row.wire_started_at+timedelta(seconds=60)
    # Simulate a long handoff/result delay without sleeping: settlement must
    # refloor the elapsed cooldown rather than allowing another immediate POST.
    with engine.begin() as c: c.exec_driver_sql("UPDATE of_delivery_policy SET next_dispatch_at=clock_timestamp()-INTERVAL '1 second'")
    authority.record_accepted(TENANT, ACCOUNT, intent, grant.token, proof)
    authority.materialize(TENANT, ACCOUNT, intent)
    with Session(engine) as s:
        assert s.get(OfDeliveryPolicy, ACCOUNT).next_dispatch_at >= utcnow()+timedelta(seconds=59)
    second = prepare(authority, "after-delayed-wire")
    assert authority.admit(TENANT, ACCOUNT, second) == "queued"
    assert len(transport.calls) == 1
    store.close()
