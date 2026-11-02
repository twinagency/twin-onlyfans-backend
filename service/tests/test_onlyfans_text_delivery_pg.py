"""PostgreSQL text-authority acceptance; disposable DB and synthetic wire only.

These cases exercise committed effects, competing processes, and interrupted
delivery. A timeout is deliberately not a negative provider receipt.
"""
from dataclasses import replace
from datetime import timedelta, timezone
import json
import multiprocessing
import time

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, due, event, seed
from db.models import Fan, Message, OfConversationState, OfCreatorBinding, OfMessageDelivery
from db.onlyfans_models import OfIngress, OfRuntime, OfWork
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfDeliveryPolicyChange, OfTextIntent
from onlyfans_runtime.store import Refused, Store, utcnow
from onlyfans_runtime.text_delivery import Accepted, Dispatch, NotAccepted, SyntheticTextTransport, TextAuthority


TENANT, ACCOUNT, FAN = "tenant-a", "acc-a", 101


def current(engine, *, mode="human_controlled", account=ACCOUNT, fan=FAN):
    """Explicit fixture evidence; seeding an account never implies recovery."""
    with Session(engine) as s, s.begin():
        runtime = s.get(OfRuntime, account)
        runtime.recovery_needed = False
        runtime.last_sync_at = utcnow()
        state = s.get(OfConversationState, (account, fan))
        state.mode = mode


def configured(database, *, mode="human_controlled", limit=20, throttle=1):
    url, engine = database
    store = seed(url, engine)
    current(engine, mode=mode)
    authority = TextAuthority(store)
    assert authority.configure(TENANT, ACCOUNT, expected_revision=0, enabled=True,
                               limit_units=limit, throttle_seconds=throttle,
                               actor="synthetic-operator", reason="Offline acceptance") == 1
    return store, authority


def prepare(authority, key="reply-1", *, fan=FAN, body="Synthetic reply", revision=0,
            origin="manual", work=None, ttl=300):
    return authority.prepare(TENANT, ACCOUNT, fan, command_key=key,
                             expected_revision=revision, body=body, origin=origin,
                             actor="synthetic-operator", work=work, ttl_seconds=ttl)


def accepted(dispatch, *, message_id=9001):
    return Accepted(account_id=dispatch.account_id, fan_id=dispatch.fan_id,
                    intent_id=dispatch.intent_id, attempt_id=dispatch.attempt_id,
                    payload_hash=dispatch.payload_hash, message_id=message_id,
                    occurred_at=utcnow().replace(tzinfo=timezone.utc).isoformat(),
                    evidence_ref="synthetic-proof:" + dispatch.attempt_id)


def rejected(dispatch):
    return NotAccepted(account_id=dispatch.account_id, fan_id=dispatch.fan_id,
                       intent_id=dispatch.intent_id, attempt_id=dispatch.attempt_id,
                       payload_hash=dispatch.payload_hash,
                       occurred_at=utcnow().replace(tzinfo=timezone.utc).isoformat(),
                       evidence_ref="synthetic-rejection:" + dispatch.attempt_id)


def unthrottle(engine):
    with engine.begin() as c:
        c.execute(text("UPDATE of_delivery_policy SET next_dispatch_at=CURRENT_TIMESTAMP-INTERVAL '1 second'"))


def assert_no_outbound(engine):
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(Message).where(Message.direction == "out")) == 0
        assert s.scalar(select(func.count()).select_from(OfMessageDelivery)) == 0


def test_manual_success_is_scoped_idempotent_and_projects_only_provider_proof(database):
    _, engine = database
    store, authority = configured(database)
    intent = prepare(authority)
    assert prepare(authority) == intent
    with pytest.raises(Refused):
        prepare(authority, body="Different immutable reply")
    assert_no_outbound(engine)
    dispatched = authority.admit(TENANT, ACCOUNT, intent)
    assert isinstance(dispatched, Dispatch)
    assert (dispatched.account_id, dispatched.fan_id, dispatched.body, dispatched.origin) == (ACCOUNT, FAN, "Synthetic reply", "manual")
    assert_no_outbound(engine)
    assert authority.record_accepted(TENANT, ACCOUNT, intent, dispatched.token, accepted(dispatched)) == "accepted"
    # Committing proof and materializing history are separate recovery steps.
    assert_no_outbound(engine)
    assert authority.materialize(TENANT, ACCOUNT, intent) is True
    authority.materialize(TENANT, ACCOUNT, intent)
    with Session(engine) as s:
        message = s.get(Message, (ACCOUNT, FAN, 9001))
        receipt = s.get(OfMessageDelivery, (ACCOUNT, FAN, 9001))
        ledger = s.get(OfTextIntent, (ACCOUNT, intent))
        policy = s.get(OfDeliveryPolicy, ACCOUNT)
        assert (message.body, message.direction) == ("Synthetic reply", "out")
        assert (receipt.outcome, receipt.origin, receipt.attempt_id) == ("sent", "human", dispatched.attempt_id)
        assert receipt.evidence_ref == "synthetic-proof:" + dispatched.attempt_id
        assert ledger.receipt_json and ledger.projected_at and ledger.reservation_held
        assert (policy.used_units, policy.queued_count, policy.active_intent_id) == (1, 0, None)
    store.close()


def test_automated_requires_current_claim_and_new_input_invalidates_queued_text(database):
    _, engine = database
    store, authority = configured(database, mode="automated")
    with pytest.raises(Refused):
        prepare(authority, origin="automated")
    store.record(TENANT, ACCOUNT, event()); store.drain(TENANT, ACCOUNT); due(engine)
    work = store.claim(TENANT)
    assert work is not None
    intent = prepare(authority, origin="automated", work=work)
    with pytest.raises(Refused):
        prepare(authority, "claim-reuse", origin="automated", work=work)
    store.record(TENANT, ACCOUNT, event(2)); store.drain(TENANT, ACCOUNT)
    assert authority.admit(TENANT, ACCOUNT, intent) == "cancelled"
    assert_no_outbound(engine)
    with Session(engine) as s:
        assert not s.get(OfTextIntent, (ACCOUNT, intent)).reservation_held
        assert s.get(OfDeliveryPolicy, ACCOUNT).used_units == 0
    store.close()


def test_automated_receipt_records_ai_origin(database):
    _, engine = database
    store, authority = configured(database, mode="automated")
    store.record(TENANT, ACCOUNT, event()); store.drain(TENANT, ACCOUNT); due(engine)
    claim = store.claim(TENANT)
    intent = prepare(authority, origin="automated", work=claim)
    transport = SyntheticTextTransport(ACCOUNT)
    assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == "accepted"
    assert len(transport.calls) == 1
    with Session(engine) as s:
        assert s.get(OfMessageDelivery, (ACCOUNT, FAN, 9001)).origin == "ai"
        assert s.get(OfWork, (ACCOUNT, FAN)).lease_token is None
    store.close()


@pytest.mark.parametrize("change", ["paused", "no_contact", "muted", "requested", "disabled", "stale_sync", "recovery", "pending", "quarantine", "retained_unknown", "blank_hold"])
def test_admission_rechecks_stop_and_recovery_state(database, change):
    _, engine = database
    store, authority = configured(database)
    intent = prepare(authority)
    with Session(engine) as s, s.begin():
        state = s.get(OfConversationState, (ACCOUNT, FAN))
        runtime = s.get(OfRuntime, ACCOUNT)
        if change in {"paused", "no_contact"}: state.mode = change
        elif change == "muted": s.get(Fan, (ACCOUNT, FAN)).is_muted = True
        elif change == "requested": state.takeover_state = "requested"
        elif change == "disabled": runtime.enabled = False
        elif change == "stale_sync": runtime.last_sync_at = utcnow() - timedelta(hours=1)
        elif change == "recovery": runtime.recovery_needed = True
        elif change == "pending": runtime.pending_events = 1
        elif change == "blank_hold": state.unresolved_attempt_id = ""
        elif change == "quarantine":
            s.add(OfIngress(account_id=ACCOUNT, binding_id="binding-" + ACCOUNT,
                            event_key="c" * 64, fan_id=FAN, message_id=7, source="rest",
                            payload="{}", status="quarantined", reason="conflicting_message_identity", received_at=utcnow()))
        elif change == "retained_unknown":
            s.add(Message(account_id=ACCOUNT, fan_id=FAN, message_id=7, direction="out", body="Historical ambiguous reply", created_at=utcnow()))
            s.flush()
            s.add(OfMessageDelivery(account_id=ACCOUNT, fan_id=FAN, message_id=7, outcome="unknown", attempt_id="old-attempt", evidence_ref="retained"))
    transport = SyntheticTextTransport(ACCOUNT)
    assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == ("queued" if change == "pending" else "refused")
    assert not transport.calls
    with Session(engine) as s:
        assert s.get(OfDeliveryPolicy, ACCOUNT).used_units == 0
        assert not s.get(OfTextIntent, (ACCOUNT, intent)).reservation_held
    store.close()


def test_prepare_also_refuses_missing_sync_no_contact_and_manual_claim(database):
    _, engine = database
    store, authority = configured(database)
    with Session(engine) as s, s.begin(): s.get(OfRuntime, ACCOUNT).last_sync_at = None
    with pytest.raises(Refused): prepare(authority)
    current(engine, mode="no_contact")
    with pytest.raises(Refused): prepare(authority)
    current(engine, mode="automated")
    store.record(TENANT, ACCOUNT, event()); store.drain(TENANT, ACCOUNT); due(engine)
    claim = store.claim(TENANT)
    current(engine, mode="human_controlled")
    with pytest.raises(Refused): prepare(authority, work=claim)
    assert_no_outbound(engine)
    store.close()


def test_control_revision_and_policy_revision_change_cancel_queued_intents(database):
    _, engine = database
    store, authority = configured(database)
    first = prepare(authority)
    store.control(TENANT, ACCOUNT, FAN, mode="paused", expected_revision=0,
                  command_key="stop", actor="synthetic-operator", reason="Stop before admission")
    assert authority.admit(TENANT, ACCOUNT, first) == "cancelled"
    store.control(TENANT, ACCOUNT, FAN, mode="human_controlled", expected_revision=1,
                  command_key="manual", actor="synthetic-operator", reason="Explicit manual control")
    second = prepare(authority, "policy-change", revision=2)
    assert authority.configure(TENANT, ACCOUNT, expected_revision=1, enabled=True,
                               limit_units=15, throttle_seconds=2, actor="operator", reason="Tighter policy") == 2
    assert authority.admit(TENANT, ACCOUNT, second) == "cancelled"
    assert_no_outbound(engine)
    store.close()


@pytest.mark.parametrize("changed", ["new_input", "expired"])
def test_manual_queued_text_expires_or_is_cancelled_by_new_input(database, changed):
    _, engine = database
    store, authority = configured(database)
    intent = prepare(authority)
    if changed == "new_input":
        store.record(TENANT, ACCOUNT, event()); store.drain(TENANT, ACCOUNT)
    else:
        with engine.begin() as c:
            c.execute(text("UPDATE of_text_intent SET expires_at=CURRENT_TIMESTAMP-INTERVAL '1 second' WHERE intent_id=:id"), {"id": intent})
    transport = SyntheticTextTransport(ACCOUNT)
    assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == "cancelled"
    assert not transport.calls
    with Session(engine) as s:
        assert s.get(OfDeliveryPolicy, ACCOUNT).used_units == 0
        assert s.get(OfTextIntent, (ACCOUNT, intent)).status == "cancelled"
    assert_no_outbound(engine)
    store.close()


@pytest.mark.parametrize("outcome", ["timeout", "malformed", "accepted_lost_response"])
def test_ambiguous_wire_holds_budget_and_never_automatically_resends(database, outcome):
    _, engine = database
    store, authority = configured(database, limit=1)
    intent = prepare(authority)
    transport = SyntheticTextTransport(ACCOUNT, outcome=outcome)
    assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == "unknown"
    assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == "unknown"
    assert len(transport.calls) == 1
    assert_no_outbound(engine)
    store.close()
    store = Store(database[0]); authority = TextAuthority(store)
    assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == "unknown"
    assert len(transport.calls) == 1
    with Session(engine) as s:
        ledger = s.get(OfTextIntent, (ACCOUNT, intent))
        policy = s.get(OfDeliveryPolicy, ACCOUNT)
        state = s.get(OfConversationState, (ACCOUNT, FAN))
        assert (ledger.status, ledger.provider_message_id, ledger.reservation_held) == ("unknown", None, True)
        assert policy.used_units == 1 and policy.active_intent_id == intent
        assert state.unresolved_attempt_id == ledger.attempt_id
        assert state.requires_attention
    store.close()


def test_expired_dispatch_becomes_unknown_and_late_matching_proof_settles(database):
    _, engine = database
    store, authority = configured(database)
    intent = prepare(authority)
    dispatched = authority.admit(TENANT, ACCOUNT, intent)
    assert isinstance(dispatched, Dispatch)
    with engine.begin() as c:
        c.execute(text("UPDATE of_text_intent SET deadline=CURRENT_TIMESTAMP-INTERVAL '1 second' WHERE intent_id=:id"), {"id": intent})
    store.close(); store = Store(database[0]); authority = TextAuthority(store)
    assert authority.expire_dispatches(TENANT, ACCOUNT) == 1
    assert authority.expire_dispatches(TENANT, ACCOUNT) == 0
    transport = SyntheticTextTransport(ACCOUNT)
    assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == "unknown"
    assert not transport.calls
    assert_no_outbound(engine)
    assert authority.record_accepted(TENANT, ACCOUNT, intent, dispatched.token, accepted(dispatched)) == "accepted"
    assert authority.repair(TENANT, ACCOUNT) == 1
    with Session(engine) as s:
        assert s.get(Message, (ACCOUNT, FAN, 9001)) is not None
        assert s.get(OfConversationState, (ACCOUNT, FAN)).unresolved_attempt_id is None
        assert s.get(OfDeliveryPolicy, ACCOUNT).used_units == 1
    store.close()


def test_proved_not_accepted_releases_reservation_once(database):
    _, engine = database
    store, authority = configured(database, limit=1)
    intent = prepare(authority)
    dispatched = authority.admit(TENANT, ACCOUNT, intent)
    proof = rejected(dispatched)
    assert authority.record_not_accepted(TENANT, ACCOUNT, intent, dispatched.token, proof) == "not_accepted"
    assert authority.record_not_accepted(TENANT, ACCOUNT, intent, dispatched.token, proof) == "not_accepted"
    assert_no_outbound(engine)
    with Session(engine) as s:
        assert s.get(OfDeliveryPolicy, ACCOUNT).used_units == 0
        assert not s.get(OfTextIntent, (ACCOUNT, intent)).reservation_held
        assert s.get(OfConversationState, (ACCOUNT, FAN)).unresolved_attempt_id is None
    unthrottle(engine)
    second = prepare(authority, "second")
    assert isinstance(authority.admit(TENANT, ACCOUNT, second), Dispatch)
    store.close()


def test_proof_identity_and_token_cannot_be_substituted(database):
    _, engine = database
    store, authority = configured(database)
    intent = prepare(authority)
    dispatched = authority.admit(TENANT, ACCOUNT, intent)
    proof = accepted(dispatched)
    with pytest.raises(Refused):
        authority.record_accepted("tenant-b", ACCOUNT, intent, dispatched.token, proof)
    with pytest.raises(Refused):
        authority.record_accepted(TENANT, ACCOUNT, intent, "wrong-token", proof)
    for bad in [replace(proof, account_id="acc-b"), replace(proof, fan_id=102),
                replace(proof, attempt_id="other-attempt"), replace(proof, payload_hash="0" * 64),
                replace(proof, message_id=0), replace(proof, occurred_at="2026-10-18T00:00:00")]:
        result = authority.record_accepted(TENANT, ACCOUNT, intent, dispatched.token, bad)
        assert result == "unknown"
    assert_no_outbound(engine)
    with Session(engine) as s:
        assert s.get(OfTextIntent, (ACCOUNT, intent)).reservation_held
        assert s.get(OfDeliveryPolicy, ACCOUNT).used_units == 1
    assert authority.record_accepted(TENANT, ACCOUNT, intent, dispatched.token, proof) == "accepted"
    store.close()


def test_stop_after_admission_retains_proof_and_external_io_holds_no_runtime_lock(database):
    _, engine = database
    store, authority = configured(database)
    intent = prepare(authority)
    def stop_on_wire(dispatched):
        # A second connection can acquire the authorization row while the
        # transport is running. Provider work must not hold a DB transaction.
        with engine.begin() as c:
            c.exec_driver_sql("SET LOCAL lock_timeout='100ms'")
            assert c.scalar(text("SELECT account_id FROM of_runtime WHERE account_id=:id FOR UPDATE NOWAIT"), {"id": ACCOUNT}) == ACCOUNT
        store.control(TENANT, ACCOUNT, FAN, mode="no_contact", expected_revision=0,
                      command_key="stop-on-wire", actor="operator", reason="Stop after admission")
    transport = SyntheticTextTransport(ACCOUNT, on_send=stop_on_wire)
    assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == "accepted"
    assert len(transport.calls) == 1
    with Session(engine) as s:
        assert s.get(Message, (ACCOUNT, FAN, 9001)).body == "Synthetic reply"
        assert s.get(OfConversationState, (ACCOUNT, FAN)).mode == "no_contact"
        assert s.get(OfMessageDelivery, (ACCOUNT, FAN, 9001)).outcome == "sent"
    store.close()


def test_revocation_before_wire_refuses_but_after_wire_retains_historical_proof(database):
    _, engine = database
    store, authority = configured(database)
    first = prepare(authority)
    with Session(engine) as s, s.begin(): s.get(OfCreatorBinding, "binding-" + ACCOUNT).revoked_at = utcnow()
    with pytest.raises(Refused): authority.admit(TENANT, ACCOUNT, first)
    with Session(engine) as s, s.begin(): s.get(OfCreatorBinding, "binding-" + ACCOUNT).revoked_at = None
    dispatched = authority.admit(TENANT, ACCOUNT, first)
    assert isinstance(dispatched, Dispatch)
    assert authority.consume_dispatch(TENANT, dispatched) == dispatched
    with Session(engine) as s, s.begin(): s.get(OfCreatorBinding, "binding-" + ACCOUNT).revoked_at = utcnow()
    assert authority.record_accepted(TENANT, ACCOUNT, first, dispatched.token, accepted(dispatched)) == "accepted"
    assert authority.materialize(TENANT, ACCOUNT, first) is True
    with Session(engine) as s:
        assert s.get(Message, (ACCOUNT, FAN, 9001)) is not None
        assert s.get(OfTextIntent, (ACCOUNT, first)).receipt_json
    store.close()


def test_projection_crash_is_repaired_without_a_second_wire_call(database, monkeypatch):
    _, engine = database
    store, authority = configured(database)
    intent = prepare(authority)
    transport = SyntheticTextTransport(ACCOUNT)
    def crash(*args, **kwargs): raise RuntimeError("synthetic crash after receipt commit")
    monkeypatch.setattr(authority, "materialize", crash)
    with pytest.raises(RuntimeError, match="receipt commit"):
        authority.dispatch(TENANT, ACCOUNT, intent, transport)
    assert len(transport.calls) == 1
    assert_no_outbound(engine)
    with Session(engine) as s:
        row = s.get(OfTextIntent, (ACCOUNT, intent))
        assert row.status == "accepted" and row.receipt_json and row.projected_at is None
    store.close(); store = Store(database[0]); authority = TextAuthority(store)
    assert authority.repair(TENANT, ACCOUNT) == 1
    assert authority.repair(TENANT, ACCOUNT) == 0
    assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == "accepted"
    assert len(transport.calls) == 1
    with Session(engine) as s: assert s.get(Message, (ACCOUNT, FAN, 9001)) is not None
    store.close()


def test_conflicting_canonical_identity_preserves_receipt_and_existing_history(database):
    _, engine = database
    store, authority = configured(database)
    intent = prepare(authority)
    dispatched = authority.admit(TENANT, ACCOUNT, intent)
    assert authority.record_accepted(TENANT, ACCOUNT, intent, dispatched.token, accepted(dispatched)) == "accepted"
    with Session(engine) as s, s.begin():
        s.add(Message(account_id=ACCOUNT, fan_id=FAN, message_id=9001, direction="out", body="Different retained history", created_at=utcnow()))
    assert authority.materialize(TENANT, ACCOUNT, intent) is False
    assert authority.repair(TENANT, ACCOUNT) == 0
    with Session(engine) as s:
        assert s.get(Message, (ACCOUNT, FAN, 9001)).body == "Different retained history"
        assert s.get(OfMessageDelivery, (ACCOUNT, FAN, 9001)) is None
        row = s.get(OfTextIntent, (ACCOUNT, intent))
        assert row.receipt_json and row.evidence_ref and row.reservation_held
        assert row.projected_at is None
        assert s.get(OfConversationState, (ACCOUNT, FAN)).requires_attention
        assert s.get(OfConversationState, (ACCOUNT, FAN)).unresolved_attempt_id == row.attempt_id
        assert s.get(OfDeliveryPolicy, ACCOUNT).active_intent_id == intent
    transport = SyntheticTextTransport(ACCOUNT)
    authority.dispatch(TENANT, ACCOUNT, intent, transport)
    assert not transport.calls
    store.close()


def test_valid_acceptance_evidence_is_immutable_when_provider_identity_collides(database):
    _, engine = database
    store, authority = configured(database)
    with Session(engine) as s, s.begin():
        s.add(Fan(account_id=ACCOUNT, fan_id=102)); s.flush()
        s.add(OfConversationState(account_id=ACCOUNT, fan_id=102, mode="human_controlled", revision=0, takeover_state="none"))
    first = prepare(authority)
    assert authority.dispatch(TENANT, ACCOUNT, first, SyntheticTextTransport(ACCOUNT)) == "accepted"
    unthrottle(engine)
    second = prepare(authority, "provider-id-collision", fan=102)
    grant = authority.admit(TENANT, ACCOUNT, second)
    assert isinstance(grant, Dispatch)
    proof = accepted(grant, message_id=9001)
    assert authority.record_accepted(TENANT, ACCOUNT, second, grant.token, proof) == "unknown"
    with Session(engine) as s:
        retained = s.get(OfTextIntent, (ACCOUNT, second)).receipt_json
        assert retained and s.get(OfDeliveryPolicy, ACCOUNT).used_units == 2
    assert authority.record_accepted(TENANT, ACCOUNT, second, grant.token, proof) == "unknown"
    with pytest.raises(Refused):
        authority.record_not_accepted(TENANT, ACCOUNT, second, grant.token, rejected(grant))
    with pytest.raises(Refused):
        authority.record_accepted(TENANT, ACCOUNT, second, grant.token, replace(proof, message_id=9002))
    with pytest.raises(Refused): authority.materialize(TENANT, ACCOUNT, second)
    transport = SyntheticTextTransport(ACCOUNT, message_id=9002)
    assert authority.dispatch(TENANT, ACCOUNT, second, transport) == "unknown"
    assert not transport.calls
    with Session(engine) as s:
        row = s.get(OfTextIntent, (ACCOUNT, second))
        assert row.receipt_json == retained and row.status == "unknown" and row.reservation_held
        assert s.get(OfDeliveryPolicy, ACCOUNT).used_units == 2
        assert s.get(OfDeliveryPolicy, ACCOUNT).active_intent_id == second
        assert s.get(OfConversationState, (ACCOUNT, 102)).unresolved_attempt_id == grant.attempt_id
        assert s.get(Message, (ACCOUNT, FAN, 9001)).body == "Synthetic reply"
        assert s.get(Message, (ACCOUNT, 102, 9001)) is None
        assert s.get(Message, (ACCOUNT, 102, 9002)) is None
    store.close()


def test_bool_recipient_is_not_proof_for_integer_recipient_one(database):
    _, engine = database
    store, authority = configured(database)
    with Session(engine) as s, s.begin():
        s.add(Fan(account_id=ACCOUNT, fan_id=1)); s.flush()
        s.add(OfConversationState(account_id=ACCOUNT, fan_id=1, mode="human_controlled", revision=0, takeover_state="none"))
    intent = prepare(authority, fan=1)
    grant = authority.admit(TENANT, ACCOUNT, intent)
    assert authority.record_accepted(TENANT, ACCOUNT, intent, grant.token, replace(accepted(grant), fan_id=True)) == "unknown"
    assert_no_outbound(engine)
    with Session(engine) as s:
        assert s.get(OfDeliveryPolicy, ACCOUNT).used_units == 1
        assert s.get(OfTextIntent, (ACCOUNT, intent)).reservation_held
    store.close()


@pytest.mark.parametrize("existing_media", ["[123]", "not-json", ""])
def test_accepted_plain_text_does_not_overwrite_hidden_or_malformed_media_identity(database, existing_media):
    _, engine = database
    store, authority = configured(database)
    intent = prepare(authority)
    grant = authority.admit(TENANT, ACCOUNT, intent)
    assert authority.record_accepted(TENANT, ACCOUNT, intent, grant.token, accepted(grant)) == "accepted"
    with Session(engine) as s, s.begin():
        s.add(Message(account_id=ACCOUNT, fan_id=FAN, message_id=9001, direction="out", body="Synthetic reply", media_ids=existing_media, media_count=0, price_cents=0, created_at=utcnow()))
    assert authority.materialize(TENANT, ACCOUNT, intent) is False
    with Session(engine) as s:
        assert s.get(Message, (ACCOUNT, FAN, 9001)).media_ids == existing_media
        assert s.get(OfMessageDelivery, (ACCOUNT, FAN, 9001)) is None
        assert s.get(OfTextIntent, (ACCOUNT, intent)).receipt_json
        assert s.get(OfTextIntent, (ACCOUNT, intent)).projected_at is None
        assert s.get(OfConversationState, (ACCOUNT, FAN)).requires_attention
    store.close()


def test_retained_unknown_receipt_without_attempt_identity_is_never_overwritten(database):
    _, engine = database
    store, authority = configured(database)
    intent = prepare(authority)
    grant = authority.admit(TENANT, ACCOUNT, intent)
    assert authority.record_accepted(TENANT, ACCOUNT, intent, grant.token, accepted(grant)) == "accepted"
    with Session(engine) as s, s.begin():
        s.add(Message(account_id=ACCOUNT, fan_id=FAN, message_id=9001, direction="out", body="Synthetic reply", created_at=utcnow()))
        s.flush()
        s.add(OfMessageDelivery(account_id=ACCOUNT, fan_id=FAN, message_id=9001, outcome="unknown", attempt_id=None, evidence_ref="retained-independent-evidence"))
    assert authority.materialize(TENANT, ACCOUNT, intent) is False
    with Session(engine) as s:
        receipt = s.get(OfMessageDelivery, (ACCOUNT, FAN, 9001))
        assert (receipt.outcome, receipt.attempt_id, receipt.evidence_ref) == ("unknown", None, "retained-independent-evidence")
        assert s.get(OfTextIntent, (ACCOUNT, intent)).receipt_json
        assert s.get(OfTextIntent, (ACCOUNT, intent)).projected_at is None
        assert s.get(OfDeliveryPolicy, ACCOUNT).active_intent_id == intent
        assert s.get(OfConversationState, (ACCOUNT, FAN)).unresolved_attempt_id == grant.attempt_id
    store.close()


@pytest.mark.parametrize("when", ["before_admission", "after_admission"])
def test_mutated_saved_text_never_reaches_wire_or_confirms_different_history(database, when):
    _, engine = database
    store, authority = configured(database)
    intent = prepare(authority)
    grant = authority.admit(TENANT, ACCOUNT, intent) if when == "after_admission" else None
    with Session(engine) as s, s.begin(): s.get(OfTextIntent, (ACCOUNT, intent)).body = "Unapproved different text"
    transport = SyntheticTextTransport(ACCOUNT)
    if grant is None:
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == "cancelled"
        with Session(engine) as s: assert s.get(OfDeliveryPolicy, ACCOUNT).used_units == 0
    else:
        assert authority.record_accepted(TENANT, ACCOUNT, intent, grant.token, accepted(grant)) == "accepted"
        assert authority.materialize(TENANT, ACCOUNT, intent) is False
        with Session(engine) as s:
            row = s.get(OfTextIntent, (ACCOUNT, intent))
            assert row.receipt_json and row.projected_at is None and row.reservation_held
            assert s.get(OfDeliveryPolicy, ACCOUNT).used_units == 1
    assert not transport.calls
    assert_no_outbound(engine)
    store.close()


def test_increasing_throttle_tightens_existing_cooldown_without_budget_reset(database):
    _, engine = database
    store, authority = configured(database, throttle=1)
    first = prepare(authority)
    assert authority.dispatch(TENANT, ACCOUNT, first, SyntheticTextTransport(ACCOUNT)) == "accepted"
    with Session(engine) as s: previous_deadline = s.get(OfDeliveryPolicy, ACCOUNT).next_dispatch_at
    assert authority.configure(TENANT, ACCOUNT, expected_revision=1, enabled=True,
                               limit_units=20, throttle_seconds=60, actor="operator", reason="Require longer spacing") == 2
    with Session(engine) as s:
        policy = s.get(OfDeliveryPolicy, ACCOUNT)
        assert policy.next_dispatch_at >= previous_deadline + timedelta(seconds=59)
        assert policy.used_units == 1
    second = prepare(authority, "after-stricter-throttle")
    transport = SyntheticTextTransport(ACCOUNT, message_id=9002)
    assert authority.dispatch(TENANT, ACCOUNT, second, transport) == "queued"
    assert not transport.calls
    store.close()


def _prepare_waiting_process(url, claim, ready, go, output):
    store = Store(url)
    try:
        authority = TextAuthority(store)
        ready.put(True)
        if not go.wait(15): raise AssertionError("synthetic start barrier timed out")
        try:
            intent = prepare(authority, origin="automated", work=claim)
            output.put(("prepared", intent))
        except Refused as exc:
            output.put(("refused", str(exc)))
    finally: store.close()


def test_claim_that_expires_while_waiting_for_account_lock_cannot_prepare(database):
    url, engine = database
    store, authority = configured(database, mode="automated")
    store.record(TENANT, ACCOUNT, event()); store.drain(TENANT, ACCOUNT); due(engine)
    claim = store.claim(TENANT)
    assert claim is not None
    ctx = multiprocessing.get_context("spawn")
    ready, output, go = ctx.Queue(), ctx.Queue(), ctx.Event()
    worker = ctx.Process(target=_prepare_waiting_process, args=(url, claim, ready, go, output))
    try:
        with engine.begin() as blocker:
            blocker.execute(text("SELECT account_id FROM of_runtime WHERE account_id=:id FOR UPDATE"), {"id": ACCOUNT})
            worker.start()
            assert ready.get(timeout=20)
            go.set()
            end = time.monotonic() + 2
            waiting = False
            while time.monotonic() < end:
                with engine.connect() as c:
                    waiting = c.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND pid<>pg_backend_pid())"))
                if waiting: break
                time.sleep(.02)
            assert waiting, "second process must actually wait for the authorization lock"
            # Make the claim expire after the old pre-lock timestamp but
            # before the second process can inspect it. This is a real DB wait.
            blocker.exec_driver_sql("SELECT pg_sleep(0.2)")
            blocker.execute(text("UPDATE of_work SET lease_until=clock_timestamp()-INTERVAL '50 milliseconds' WHERE account_id=:id"), {"id": ACCOUNT})
        status, reason = output.get(timeout=20)
        worker.join(15)
        assert worker.exitcode == 0
        assert status == "refused" and "claim" in reason
        with Session(engine) as s:
            assert s.scalar(select(func.count()).select_from(OfTextIntent)) == 0
            assert s.get(OfDeliveryPolicy, ACCOUNT).used_units == 0
        assert_no_outbound(engine)
    finally:
        if worker.is_alive(): worker.terminate(); worker.join(5)
        store.close()


def _dispatch_process(url, intent, ready, go, output):
    store = Store(url)
    try:
        authority = TextAuthority(store)
        transport = SyntheticTextTransport(ACCOUNT)
        ready.put(True)
        if not go.wait(15): raise AssertionError("synthetic start barrier timed out")
        status = authority.dispatch(TENANT, ACCOUNT, intent, transport)
        output.put((status, len(transport.calls)))
    finally: store.close()


def test_two_processes_dispatch_one_intent_and_reserve_one_unit(database):
    url, engine = database
    store, authority = configured(database)
    intent = prepare(authority)
    ctx = multiprocessing.get_context("spawn")
    ready, output, go = ctx.Queue(), ctx.Queue(), ctx.Event()
    workers = [ctx.Process(target=_dispatch_process, args=(url, intent, ready, go, output)) for _ in range(2)]
    for worker in workers: worker.start()
    try:
        for _ in workers: assert ready.get(timeout=20)
        go.set()
        results = [output.get(timeout=20) for _ in workers]
        for worker in workers: worker.join(15); assert worker.exitcode == 0
        assert sum(calls for _, calls in results) == 1
        assert {status for status, _ in results} <= {"accepted", "dispatching"}
        with Session(engine) as s:
            assert s.scalar(select(func.count()).select_from(Message).where(Message.direction == "out")) == 1
            assert s.get(OfDeliveryPolicy, ACCOUNT).used_units == 1
            assert s.scalar(select(func.count()).select_from(OfTextIntent)) == 1
    finally:
        for worker in workers:
            if worker.is_alive(): worker.terminate(); worker.join(5)
        store.close()


def test_cumulative_budget_and_throttle_persist_across_restart_and_policy_changes(database):
    url, engine = database
    store, authority = configured(database, limit=2, throttle=60)
    first = prepare(authority)
    assert authority.dispatch(TENANT, ACCOUNT, first, SyntheticTextTransport(ACCOUNT)) == "accepted"
    second = prepare(authority, "second")
    store.close(); store = Store(url); authority = TextAuthority(store)
    assert authority.admit(TENANT, ACCOUNT, second) == "queued", "restart cannot erase account throttle"
    unthrottle(engine)
    assert authority.dispatch(TENANT, ACCOUNT, second, SyntheticTextTransport(ACCOUNT, message_id=9002)) == "accepted"
    assert authority.configure(TENANT, ACCOUNT, expected_revision=1, enabled=True,
                               limit_units=1, throttle_seconds=1, actor="operator", reason="Lower limit retains actual use") == 2
    with Session(engine) as s:
        policy = s.get(OfDeliveryPolicy, ACCOUNT)
        assert (policy.revision, policy.limit_units, policy.used_units) == (2, 1, 2)
        assert s.scalar(select(func.count()).select_from(OfDeliveryPolicyChange)) == 2
    assert authority.configure(TENANT, ACCOUNT, expected_revision=2, enabled=False,
                               limit_units=1, throttle_seconds=1, actor="operator", reason="Paused without reset") == 3
    assert authority.configure(TENANT, ACCOUNT, expected_revision=3, enabled=True,
                               limit_units=1, throttle_seconds=1, actor="operator", reason="Resume without reset") == 4
    with pytest.raises(Refused):
        authority.configure(TENANT, ACCOUNT, expected_revision=1, enabled=True,
                            limit_units=100, throttle_seconds=1, actor="operator", reason="Stale revision")
    unthrottle(engine)
    third = prepare(authority, "third")
    transport = SyntheticTextTransport(ACCOUNT, message_id=9003)
    assert authority.dispatch(TENANT, ACCOUNT, third, transport) == "refused"
    assert not transport.calls
    with Session(engine) as s:
        policy = s.get(OfDeliveryPolicy, ACCOUNT)
        assert (policy.revision, policy.used_units) == (4, 2)
        changes = s.scalars(select(OfDeliveryPolicyChange).order_by(OfDeliveryPolicyChange.revision)).all()
        assert [x.revision for x in changes] == [1, 2, 3, 4]
        assert all(x.actor and x.reason and json.loads(x.before_json) is not None and json.loads(x.after_json) is not None for x in changes)
    store.close()


def test_unresolved_account_attempt_blocks_other_fan_and_unknown_cannot_release_cap(database):
    _, engine = database
    store, authority = configured(database)
    with Session(engine) as s, s.begin():
        s.add(Fan(account_id=ACCOUNT, fan_id=102)); s.flush()
        s.add(OfConversationState(account_id=ACCOUNT, fan_id=102, mode="human_controlled", revision=0, takeover_state="none"))
    first = prepare(authority)
    second = prepare(authority, "other-fan", fan=102)
    dispatched = authority.admit(TENANT, ACCOUNT, first)
    assert authority.admit(TENANT, ACCOUNT, second) == "queued"
    assert authority.mark_unknown(TENANT, ACCOUNT, first, dispatched.token) == "unknown"
    unthrottle(engine)
    assert authority.admit(TENANT, ACCOUNT, second) == "queued"
    assert authority.configure(TENANT, ACCOUNT, expected_revision=1, enabled=False,
                               limit_units=20, throttle_seconds=1, actor="operator", reason="Pause ambiguous wire") == 2
    with Session(engine) as s:
        assert s.get(OfDeliveryPolicy, ACCOUNT).used_units == 1
        assert s.get(OfDeliveryPolicy, ACCOUNT).active_intent_id == first
        assert s.get(OfTextIntent, (ACCOUNT, first)).reservation_held
    assert_no_outbound(engine)
    store.close()


def test_transport_registration_is_exact_and_account_bound(database):
    _, engine = database
    store, authority = configured(database)
    intent = prepare(authority)
    other = SyntheticTextTransport("acc-b")
    with pytest.raises(Refused): authority.dispatch(TENANT, ACCOUNT, intent, other)
    class PretendTransport:
        account_id = ACCOUNT
        def send(self, dispatch): raise AssertionError("Unregistered wire must never run")
    with pytest.raises(Refused): authority.dispatch(TENANT, ACCOUNT, intent, PretendTransport())
    class DerivedTransport(SyntheticTextTransport): pass
    with pytest.raises(Refused): authority.dispatch(TENANT, ACCOUNT, intent, DerivedTransport(ACCOUNT))
    assert not other.calls
    assert_no_outbound(engine)
    with Session(engine) as s: assert s.get(OfDeliveryPolicy, ACCOUNT).used_units == 0
    store.close()

