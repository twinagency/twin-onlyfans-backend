"""Synthetic archive proof in uniquely named disposable PostgreSQL databases.

No provider transport, account credentials, model calls or fan sends. Explicit
owned-table creation here is test-only; production migration is Claude-owned.
"""
from dataclasses import replace
from datetime import timedelta, timezone

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, due, event, seed
from db.models import Base, Fan, Message, OfConversationState, OfCreatorBinding
from db.onlyfans_models import OfIngress, OfRuntime, OfWork
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_offer_import_models import OfOfferObservation
from onlyfans_read.agency_scope import AgencyScope, current_scope
from onlyfans_runtime.launch_history_memory.recovery import (
    HistoryRecovery, OfHistoryCursor, OfHistoryScan, RecoveryBase, affects_recent_context,
)
from onlyfans_runtime.offer_terms import OfferObservation
from onlyfans_runtime.store import Event, Refused, Store, utcnow
from onlyfans_runtime.worker import ChatPage, MessagePage

TENANT, ACCOUNT, FAN = "tenant-a", "acc-a", 101


def history(mid, fan=FAN, *, body=None, days=2, direction="in", media_ids=(), price_cents=0):
    return Event(fan, mid, (utcnow() - timedelta(days=days)).replace(tzinfo=timezone.utc).isoformat(),
                 direction, body or f"Synthetic archive {mid}", media_ids, price_cents)


@pytest.fixture
def ready(database):
    url, engine = database
    assert not set(RecoveryBase.metadata.tables) & set(Base.metadata.tables)
    RecoveryBase.metadata.create_all(engine)  # Only this fixture's owned tables.
    store = seed(url, engine)
    with Session(engine) as session, session.begin():
        session.get(OfRuntime, ACCOUNT).recovery_needed = False
    try:
        yield store, HistoryRecovery(store), engine
    finally:
        store.close()


def release_retry(engine):
    with Session(engine) as session, session.begin():
        for model in (OfHistoryCursor, OfHistoryScan):
            for row in session.scalars(select(model)).all():
                row.retry_at = None


def test_restart_resumes_exact_cursor_and_import_is_always_historical(ready):
    store, recovery, engine = ready
    recovery.request_fan(TENANT, ACCOUNT, FAN)
    first = recovery.claim(TENANT, ACCOUNT)
    assert recovery.page(TENANT, first, [history(9, days=0), history(8)], has_more=True) == "continue"
    store.close()
    restarted = Store(engine.url.render_as_string(hide_password=False))
    try:
        again = HistoryRecovery(restarted)
        again.request_fan(TENANT, ACCOUNT, FAN)
        second = again.claim(TENANT, ACCOUNT)
        assert second.before_id == 8 and second.generation == first.generation + 1
        assert again.page(TENANT, second, [history(7), history(6)], has_more=False) == "complete"
        assert again.claim(TENANT, ACCOUNT) is None
        progress = again.progress(TENANT, ACCOUNT, fan_id=FAN)
        assert progress["status"] == "complete" and progress["messagesAdded"] == 4
        assert progress["pagesCommitted"] == 2 and progress["beforeId"] == "6"
        assert progress["percentComplete"] is None and progress["totalMessages"] is None
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(Message)) == 4
            assert session.get(OfWork, (ACCOUNT, FAN)) is None
            assert session.get(OfRuntime, ACCOUNT).pending_events == 0
            assert session.get(OfRuntime, ACCOUNT).recovery_needed is False
            assert set(session.scalars(select(OfIngress.source))) == {"import"}
    finally:
        restarted.close()


def test_expired_claim_is_fenced_and_other_fans_get_a_turn(ready):
    _, recovery, engine = ready
    for fan in (FAN, 202):
        recovery.request_fan(TENANT, ACCOUNT, fan)
    stale = recovery.claim(TENANT, ACCOUNT)
    with Session(engine) as session, session.begin():
        session.get(OfHistoryCursor, (ACCOUNT, FAN)).lease_until = utcnow() - timedelta(seconds=1)
    other = recovery.claim(TENANT, ACCOUNT)
    assert other.fan_id == 202
    current = recovery.claim(TENANT, ACCOUNT)
    assert current.fan_id == FAN and current.generation > stale.generation
    with pytest.raises(Refused, match="stale"):
        recovery.page(TENANT, stale, [history(3)], has_more=False)
    assert recovery.page(TENANT, current, [history(3)], has_more=True) == "continue"
    recovery.page(TENANT, other, [], has_more=False)
    assert recovery.claim(TENANT, ACCOUNT).before_id == 3


@pytest.mark.parametrize("bad", ["ascending", "duplicate", "wrong_fan", "cursor", "empty_more", "false_type", "future", "oversize"])
def test_invalid_page_never_advances_or_partially_applies(ready, bad):
    _, recovery, engine = ready
    recovery.request_fan(TENANT, ACCOUNT, FAN, before_id=20)
    claim = recovery.claim(TENANT, ACCOUNT)
    rows, more = [history(19), history(18)], False
    if bad == "ascending": rows = list(reversed(rows))
    elif bad == "duplicate": rows[1] = rows[0]
    elif bad == "wrong_fan": rows[1] = history(18, fan=202)
    elif bad == "cursor": rows[0] = history(21)
    elif bad == "empty_more": rows, more = [], True
    elif bad == "false_type": more = 0
    elif bad == "future": rows[1] = history(18, days=-1)
    elif bad == "oversize": rows = [history(mid) for mid in range(119, 18, -1)]
    with pytest.raises(Refused):
        recovery.page(TENANT, claim, rows, has_more=more)
    with Session(engine) as session:
        row = session.get(OfHistoryCursor, (ACCOUNT, FAN))
        assert row.before_id == 20 and row.pages_completed == 0 and row.status == "running"
        assert session.scalar(select(func.count()).select_from(Message)) == 0
        assert session.scalar(select(func.count()).select_from(OfIngress)) == 0


@pytest.mark.parametrize("conflicting_field", ["body", "created_at"])
def test_duplicate_replay_is_canonical_and_conflict_never_claims_completion(ready, conflicting_field):
    store, recovery, engine = ready
    original = history(8)
    store.record(TENANT, ACCOUNT, original, source="import")
    store.drain(TENANT, ACCOUNT)
    recovery.request_fan(TENANT, ACCOUNT, FAN)
    claim = recovery.claim(TENANT, ACCOUNT)
    recovery.page(TENANT, claim, [original], has_more=True)
    with Session(engine) as session:
        assert session.get(OfHistoryCursor, (ACCOUNT, FAN)).messages_added == 0
        assert session.scalar(select(func.count()).select_from(Message)) == 1
    conflict = history(7)
    store.record(TENANT, ACCOUNT, conflict, source="import")
    store.drain(TENANT, ACCOUNT)
    claim = recovery.claim(TENANT, ACCOUNT)
    changed = replace(conflict, **({"body": "Contradictory immutable payload"} if conflicting_field == "body"
                                  else {"created_at": history(7, days=3).created_at}))
    assert recovery.page(TENANT, claim, [changed], has_more=False) == "conflict"
    with Session(engine) as session:
        row = session.get(OfHistoryCursor, (ACCOUNT, FAN))
        assert row.before_id == 8 and row.completed_at is None
        assert session.get(Message, (ACCOUNT, FAN, 7)).body == conflict.body
        assert row.last_error == "conflicting_history"
        assert session.get(OfRuntime, ACCOUNT).recovery_needed is True
        assert session.get(OfConversationState, (ACCOUNT, FAN)).requires_attention
        assert session.scalar(select(OfIngress).where(OfIngress.status == "quarantined")).reason == "conflicting_message_identity"


def test_live_input_wins_during_provider_read_and_history_never_replies(ready):
    store, recovery, engine = ready
    recovery.request_fan(TENANT, ACCOUNT, FAN)
    claim = recovery.claim(TENANT, ACCOUNT)
    store.record(TENANT, ACCOUNT, event(50))
    store.drain(TENANT, ACCOUNT)
    due(engine)
    live = store.claim(TENANT)
    assert live is not None
    assert recovery.page(TENANT, claim, [history(9)], has_more=True) == "live_input_pending"
    assert recovery.claim(TENANT, ACCOUNT) is None
    assert recovery.progress(TENANT, ACCOUNT)["conversationsDeferredForLiveInput"] == 1
    store.complete(TENANT, live)  # Exercises existing claim fence; sends nothing.
    release_retry(engine)
    next_claim = recovery.claim(TENANT, ACCOUNT)
    assert recovery.page(TENANT, next_claim, [history(9)], has_more=False) == "complete"
    with Session(engine) as session:
        work = session.get(OfWork, (ACCOUNT, FAN))
        assert work.status == "done" and work.target_message_id == 50
        assert session.get(OfRuntime, ACCOUNT).recovery_needed is False
        assert session.get(Message, (ACCOUNT, FAN, 9)) is not None


def test_pending_live_duplicate_is_not_consumed_as_history(ready):
    store, recovery, engine = ready
    recovery.request_fan(TENANT, ACCOUNT, FAN)
    incoming = event(42)
    store.record(TENANT, ACCOUNT, incoming)
    claim = recovery.claim(TENANT, ACCOUNT)
    assert recovery.page(TENANT, claim, [incoming], has_more=False) == "live_input_pending"
    assert store.drain(TENANT, ACCOUNT) == 1
    with Session(engine) as session:
        assert session.get(OfWork, (ACCOUNT, FAN)).status == "waiting"
        assert session.get(OfHistoryCursor, (ACCOUNT, FAN)).pages_completed == 0


def test_disabled_account_can_backfill_despite_stale_waiting_work(ready):
    store, recovery, engine = ready
    store.record(TENANT, ACCOUNT, event(42))
    store.drain(TENANT, ACCOUNT)
    with Session(engine) as session, session.begin():
        session.get(OfRuntime, ACCOUNT).enabled = False
    recovery.request_fan(TENANT, ACCOUNT, FAN)
    claim = recovery.claim(TENANT, ACCOUNT)
    assert claim is not None
    assert recovery.page(TENANT, claim, [history(9)], has_more=False) == "complete"
    with Session(engine) as session:
        assert session.get(OfRuntime, ACCOUNT).enabled is False
        assert session.get(OfWork, (ACCOUNT, FAN)).target_message_id == 42


def test_pressure_preserves_cursor_and_retry_does_not_set_recent_recovery(ready):
    _, recovery, engine = ready
    recovery.request_fan(TENANT, ACCOUNT, FAN)
    claim = recovery.claim(TENANT, ACCOUNT)
    with Session(engine) as session, session.begin():
        session.get(OfRuntime, ACCOUNT).pending_events = 900
    assert recovery.page(TENANT, claim, [history(2)], has_more=False) == "backpressure"
    assert recovery.claim(TENANT, ACCOUNT) is None
    with Session(engine) as session, session.begin():
        assert not session.get(OfRuntime, ACCOUNT).recovery_needed
        assert session.get(OfHistoryCursor, (ACCOUNT, FAN)).before_id is None
        session.get(OfRuntime, ACCOUNT).pending_events = 0
    release_retry(engine)
    assert recovery.page(TENANT, recovery.claim(TENANT, ACCOUNT), [history(2)], has_more=False) == "complete"


def test_account_discovery_and_known_fan_coverage_resume_independently(ready):
    store, recovery, engine = ready
    recovery.request_account(TENANT, ACCOUNT)
    claim = recovery.claim_scan(TENANT, ACCOUNT)
    recovery.scan_page(TENANT, claim, [202], next_cursor="1")
    recovery = HistoryRecovery(store)
    recovery.request_account(TENANT, ACCOUNT)
    next_claim = recovery.claim_scan(TENANT, ACCOUNT)
    assert next_claim.cursor == "1"
    recovery.scan_page(TENANT, next_claim, [303], next_cursor=None)
    progress = recovery.progress(TENANT, ACCOUNT)
    assert progress["discoveryComplete"] and progress["status"] == "incomplete"
    assert progress["conversationsDiscovered"] == 3  # Includes stored archived fan 101.
    for _ in range(3):
        recovery.page(TENANT, recovery.claim(TENANT, ACCOUNT), [], has_more=False)
    assert recovery.progress(TENANT, ACCOUNT)["status"] == "complete"
    with Session(engine) as session:
        assert session.get(OfRuntime, ACCOUNT).recovery_needed is False


@pytest.mark.parametrize("cursor", ["0", "2", "-1", "1x", "01", "1000001"])
def test_discovery_cursor_must_strictly_advance_by_validated_rows(ready, cursor):
    _, recovery, engine = ready
    recovery.request_account(TENANT, ACCOUNT)
    claim = recovery.claim_scan(TENANT, ACCOUNT)
    with pytest.raises(Refused, match="cursor"):
        recovery.scan_page(TENANT, claim, [202], next_cursor=cursor)
    with Session(engine) as session:
        assert session.get(OfHistoryScan, ACCOUNT).page_count == 0
        assert session.get(OfHistoryCursor, (ACCOUNT, 202)) is None


def test_creator_fan_account_isolation_and_binding_revocation(ready):
    store, recovery, engine = ready
    other = seed(engine.url.render_as_string(hide_password=False), engine, "acc-b", "tenant-b")
    try:
        recovery.request_fan(TENANT, ACCOUNT, FAN)
        recovery.request_fan("tenant-b", "acc-b", FAN)
        claim = recovery.claim(TENANT, ACCOUNT)
        with pytest.raises(Refused, match="scope"):
            recovery.page("tenant-b", claim, [history(3)], has_more=False)
        with pytest.raises(Refused, match="scope"):
            recovery.progress("tenant-b", ACCOUNT)
        token = current_scope.set(AgencyScope(("creator-acc-b",), "synthetic-actor", True))
        try:
            with pytest.raises(Refused, match="scope"):
                recovery.progress(TENANT, ACCOUNT)
        finally:
            current_scope.reset(token)
        recovery.page(TENANT, claim, [history(3)], has_more=False)
        assert recovery.progress("tenant-b", "acc-b", fan_id=FAN)["messagesAdded"] == 0
        with Session(engine) as session, session.begin():
            assert session.get(Message, ("acc-b", FAN, 3)) is None
            session.get(OfCreatorBinding, "binding-acc-b").revoked_at = utcnow()
        with pytest.raises(Refused, match="scope"):
            recovery.claim("tenant-b", "acc-b")
    finally:
        other.close()


def test_connection_revision_fences_result_after_external_read(ready, monkeypatch):
    _, recovery, engine = ready
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    with Session(engine) as session, session.begin():
        session.add(OfProviderConnection(account_id=ACCOUNT, binding_id="binding-acc-a", owner_id=999,
            revision=1, sealed_config="synthetic-never-decrypted", expires_at=utcnow()+timedelta(hours=1),
            status="verified", verified_at=utcnow(), updated_at=utcnow()))
    recovery.request_fan(TENANT, ACCOUNT, FAN, connection_revision=1)
    claim = recovery.claim(TENANT, ACCOUNT, connection_revision=1)
    with Session(engine) as session, session.begin():
        session.get(OfProviderConnection, ACCOUNT).revision = 2
    with pytest.raises(Refused, match="current"):
        recovery.page(TENANT, claim, [history(5)], has_more=False)
    with Session(engine) as session:
        assert session.get(Message, (ACCOUNT, FAN, 5)) is None


def test_exact_offer_observation_is_preserved_atomically(ready):
    _, recovery, engine = ready
    paid = history(40, direction="out", media_ids=(8, 9), price_cents=2000)
    observation = OfferObservation("message", 40, FAN, "direct", paid.created_at, (8, 9), (8,), False, 2000)
    recovery.request_fan(TENANT, ACCOUNT, FAN)
    recovery.page(TENANT, recovery.claim(TENANT, ACCOUNT), [paid], has_more=False, observations=[observation])
    with Session(engine) as session:
        row = session.scalar(select(OfOfferObservation).where(OfOfferObservation.account_id == ACCOUNT))
        assert row.price_cents == 2000 and row.preview_json == "[8]" and row.locked_text is False
        assert session.get(Message, (ACCOUNT, FAN, 40)).price_cents == 2000
        assert session.get(OfWork, (ACCOUNT, FAN)) is None


def test_wrong_offer_evidence_rolls_back_page(ready):
    _, recovery, engine = ready
    paid = history(40, direction="out", media_ids=(8, 9), price_cents=2000)
    wrong = OfferObservation("message", 40, FAN, "direct", paid.created_at, (8, 9), (), False, 3000)
    recovery.request_fan(TENANT, ACCOUNT, FAN)
    claim = recovery.claim(TENANT, ACCOUNT)
    with pytest.raises(Refused, match="outside"):
        recovery.page(TENANT, claim, [paid], has_more=False, observations=[wrong])
    with Session(engine) as session:
        assert session.get(Message, (ACCOUNT, FAN, 40)) is None
        assert session.scalar(select(func.count()).select_from(OfOfferObservation)) == 0


def test_step_has_no_transaction_during_read_and_failure_is_retryable(ready):
    _, recovery, engine = ready
    recovery.request_fan(TENANT, ACCOUNT, FAN)

    class Reader:
        account_id = ACCOUNT
        calls = 0

        def messages(self, **kwargs):
            self.calls += 1
            with engine.begin() as connection:
                assert connection.scalar(text("SELECT account_id FROM of_runtime WHERE account_id=:account FOR UPDATE NOWAIT"),
                                         {"account": ACCOUNT}) == ACCOUNT
            if self.calls == 1:
                raise RuntimeError("synthetic failure; this text must not be persisted")
            return MessagePage([history(4)], False)

    reader = Reader()
    with pytest.raises(RuntimeError):
        recovery.step(TENANT, ACCOUNT, reader)
    progress = recovery.progress(TENANT, ACCOUNT, fan_id=FAN)
    assert progress["lastError"] == "provider_read_failed" and progress["beforeId"] is None
    assert recovery.step(TENANT, ACCOUNT, reader) == 0 and reader.calls == 1
    release_retry(engine)
    assert recovery.step(TENANT, ACCOUNT, reader) == 1 and reader.calls == 2
    assert recovery.progress(TENANT, ACCOUNT, fan_id=FAN)["status"] == "complete"
    reader.account_id = "acc-b"
    with pytest.raises(Refused, match="scope"):
        recovery.step(TENANT, ACCOUNT, reader)


def test_discovery_failure_does_not_starve_known_fan_archive(ready):
    _, recovery, _ = ready
    recovery.request_account(TENANT, ACCOUNT)

    class Reader:
        account_id = ACCOUNT

        def chats(self, **kwargs):
            raise RuntimeError("Synthetic discovery failure")

        def messages(self, **kwargs):
            return MessagePage([history(9)], False)

    with pytest.raises(RuntimeError, match="discovery"):
        recovery.step(TENANT, ACCOUNT, Reader())
    assert recovery.progress(TENANT, ACCOUNT, fan_id=FAN)["status"] == "complete"
    account = recovery.progress(TENANT, ACCOUNT)
    assert account["status"] == "incomplete" and account["discoveryLastError"] == "provider_read_failed"


def test_recent_context_predicate_preserves_latest_100_and_is_fan_scoped(ready):
    _, _, engine = ready
    stamp = utcnow() - timedelta(hours=1)
    with Session(engine) as session, session.begin():
        for mid in range(1, 101):
            session.add(Message(account_id=ACCOUNT, fan_id=FAN, message_id=mid, direction="in",
                                body="Synthetic recent input", created_at=stamp + timedelta(seconds=mid)))
    with Session(engine) as session:
        assert affects_recent_context(session, ACCOUNT, FAN, history(1000)) is False
        assert affects_recent_context(session, ACCOUNT, FAN, event(1000)) is True
        assert affects_recent_context(session, ACCOUNT, 202, history(1, fan=202)) is True
        with pytest.raises(Refused, match="scope"):
            affects_recent_context(session, ACCOUNT, FAN, history(1, fan=202))
    with Session(engine) as session, session.begin():
        session.get(Message, (ACCOUNT, FAN, 100)).is_unsent = True
    with Session(engine) as session:
        assert affects_recent_context(session, ACCOUNT, FAN, history(1000)) is True


def test_requested_recent_boundary_cannot_be_silently_rewritten(ready):
    _, recovery, _ = ready
    recovery.request_fan(TENANT, ACCOUNT, FAN, before_id=20)
    recovery.request_fan(TENANT, ACCOUNT, FAN, before_id=20)
    with pytest.raises(Refused, match="boundary"):
        recovery.request_fan(TENANT, ACCOUNT, FAN, before_id=30)
    claim = recovery.claim(TENANT, ACCOUNT)
    assert claim.before_id == 20
