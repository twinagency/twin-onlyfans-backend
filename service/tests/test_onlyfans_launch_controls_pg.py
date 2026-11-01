"""Combined operator projections and commands over disposable PostgreSQL only."""
from datetime import datetime, timedelta
import json
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from test_onlyfans_launch_engagement_pg import schema_template, database  # noqa: F401
from test_onlyfans_launch_engagement_pg import rig, schedule  # noqa: F401
from test_onlyfans_launch_integration_pg import prepare as native_prepare
from test_onlyfans_foundation_pg import seed
from test_onlyfans_connections_pg import verified
from test_onlyfans_ledger_pg import parsed, record, transaction
from db.models import AccountAiConfig, Fan, Message, OfCreatorBinding, Transaction, VaultSend
from db.onlyfans_ledger_models import OfLedgerEvidence, OfLedgerScan
from db.onlyfans_models import OfRuntime
from onlyfans_commands.launch_api import engagement_router, history_router
from onlyfans_commands.launch_control_api import read_router, control_router
from onlyfans_read.agency_scope import AgencyScope, AgencyScopeMiddleware, current_scope
from onlyfans_runtime.launch_engagement.models import Action
from onlyfans_runtime.launch_history_memory.memory import HistoricalMemory
from onlyfans_runtime.launch_operations import account_state, control_account, ledger_status, launch_readiness, resolve_ledger
from onlyfans_runtime.launch_ledger.dashboard import read_ledger, retry as retry_ledger
from onlyfans_runtime.launch_ledger.models import LedgerRetryCommand
from onlyfans_runtime.launch_operations_models import OfLaunchCommand
from onlyfans_runtime.native_context import canonical
from onlyfans_runtime.store import Refused, utcnow
from onlyfans_runtime.text_delivery import SyntheticTextTransport, TextAuthority

ACTOR = "op-session:" + "a" * 64
ACCOUNT, TENANT, FAN = "acc-a", "tenant-a", 101


from onlyfans_runtime.ledger import _coverage_receipt

def client(store, monkeypatch):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "api")
    for purpose, token in (("CONFIGURATION", "config-synthetic"), ("CONTROL", "control-synthetic"), ("API", "read-synthetic")):
        monkeypatch.setenv("ONLYFANS_" + purpose + "_TOKEN", token)
        monkeypatch.setenv("ONLYFANS_" + purpose + "_TENANT", TENANT)
    app = FastAPI()
    app.state.control_store = store
    app.add_middleware(AgencyScopeMiddleware)
    for router in (engagement_router, history_router, read_router, control_router):
        app.include_router(router)
    return TestClient(app)


def header(purpose="configuration"):
    values = {"configuration": ("config-synthetic", "Configuration", "of-configuration.v3"),
        "control": ("control-synthetic", "Control", "of-controls.v1"),
        "read": ("read-synthetic", "Schema", "of-read.v1")}
    token, name, version = values[purpose]
    if purpose == "read":
        from onlyfans_read.contract import SCHEMA_VERSION
        version = SCHEMA_VERSION
    return {"Authorization": "Bearer " + token, "X-OnlyFans-" + name + "-Version": version}


def test_memory_fan_pages_are_bounded_stable_and_rebinding_invalidates_cursor(database):
    store = seed(*database)
    try:
        with Session(database[1]) as s, s.begin():
            for fan in range(1000, 1125):
                s.add(Fan(account_id=ACCOUNT, fan_id=fan, of_display_name="Synthetic fan"))
            s.add(Message(account_id=ACCOUNT, fan_id=FAN, message_id=1, body="Hello", direction="in", created_at=utcnow()))
        memory = HistoricalMemory(store)
        seen, cursor = [], None
        while True:
            page = memory.list_fans(TENANT, ACCOUNT, cursor=cursor, limit=40)
            seen += [row["fanId"] for row in page["fans"]]
            if cursor is None:
                first_cursor = page["nextCursor"]
                assert page["fans"][0]["storedMessages"] == 1
                with Session(database[1]) as s, s.begin():
                    s.add(Fan(account_id=ACCOUNT, fan_id=9999))
            cursor = page["nextCursor"]
            if cursor is None:
                break
        assert len(seen) == len(set(seen)) == 126 and "9999" not in seen
        with pytest.raises(Refused):
            memory.list_fans("other-tenant", ACCOUNT, cursor=first_cursor)
        with Session(database[1]) as s, s.begin():
            old = s.get(OfCreatorBinding, "binding-" + ACCOUNT)
            old.revoked_at = utcnow()
            s.flush()
            s.add(OfCreatorBinding(id="new-binding", tenant_id=TENANT, creator_id="new-creator", account_id=ACCOUNT))
            s.flush()
            s.get(OfRuntime, ACCOUNT).binding_id = "new-binding"
        with pytest.raises(Refused, match="cursor scope"):
            memory.list_fans(TENANT, ACCOUNT, cursor=first_cursor)
    finally:
        store.close()


def test_real_memory_api_serializes_populated_postgresql_summary_and_keeps_ids_exact(database, monkeypatch):
    from onlyfans_runtime.launch_history_memory.recovery import OfHistoryCursor, OfHistoryScan
    store = seed(*database)
    try:
        large_fan = 9007199254740993
        now = utcnow()
        with Session(store.engine) as s, s.begin():
            s.add(Fan(account_id=ACCOUNT, fan_id=large_fan))
            s.flush()
            for fan, pages, observed, added in ((FAN, 2, 41, 31), (large_fan, 3, 43, 33)):
                s.add(Message(account_id=ACCOUNT, fan_id=fan, message_id=1,
                    body="Synthetic retained history", direction="in", created_at=now))
                s.add(OfHistoryCursor(account_id=ACCOUNT, fan_id=fan, binding_id="binding-" + ACCOUNT,
                    status="complete", pages_completed=pages, messages_observed=observed,
                    messages_added=added, completed_at=now, updated_at=now))
            s.add(OfHistoryScan(account_id=ACCOUNT, binding_id="binding-" + ACCOUNT,
                status="complete", known_fans_complete=True, completed_at=now, updated_at=now))
        monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
        for fan in (FAN, large_fan):
            assert HistoricalMemory(store).process(TENANT, ACCOUNT, fan)["processed"] == 1
        browser = client(store, monkeypatch)
        prefix = f"/onlyfans/configuration/v1/accounts/{ACCOUNT}/history-memory"
        listed = browser.get(prefix + "/fans?limit=50", headers=header())
        assert listed.status_code == 200, listed.text
        fans = listed.json()["fans"]
        assert [row["fanId"] for row in fans] == [str(FAN), str(large_fan)]
        assert all(type(row["storedMessages"]) is int and row["processedMessages"] == 1 for row in fans)
        account = browser.get(prefix + "/progress", headers=header())
        assert account.status_code == 200, account.text
        progress = account.json()["progress"]
        assert (progress["pagesCommitted"], progress["messagesObserved"], progress["messagesAdded"]) == (5, 84, 64)
        assert all(type(progress[key]) is int for key in ("pagesCommitted", "messagesObserved", "messagesAdded"))
        detail = browser.get(prefix + f"/fans/{large_fan}/progress", headers=header())
        assert detail.status_code == 200, detail.text
        assert detail.json()["recovery"]["fanId"] == str(large_fan)
        assert detail.json()["recovery"]["messagesObserved"] == 43
    finally:
        store.close()


def test_engagement_actions_page_equal_timestamps_without_mutating_rows(database, monkeypatch):
    store = seed(*database)
    try:
        now = utcnow()
        with Session(database[1]) as s, s.begin():
            for index in range(125):
                fan = 1000 + index
                s.add(Fan(account_id=ACCOUNT, fan_id=fan))
                s.flush()
                s.add(Action(account_id=ACCOUNT, action_id=str(uuid.UUID(int=index + 1)),
                    binding_id="binding-" + ACCOUNT, fan_id=fan, kind="offer", anchor_message_id=1,
                    command_key=str(index), request_hash="a" * 64, snapshot_json="{}", snapshot_hash="b" * 64,
                    status="cancelled", due_at=now, expires_at=now + timedelta(hours=1), created_at=now, updated_at=now))
        browser = client(store, monkeypatch)
        path = f"/onlyfans/configuration/v1/accounts/{ACCOUNT}/engagement/actions"
        cursor, seen = None, []
        while True:
            page = browser.get(path, params={"limit": 40, **({"cursor": cursor} if cursor else {})}, headers=header())
            assert page.status_code == 200, page.text
            payload = page.json()
            seen.extend(row["actionId"] for row in payload["actions"])
            if cursor is None:
                with Session(database[1]) as s, s.begin():
                    s.add(Fan(account_id=ACCOUNT, fan_id=2000))
                    s.flush()
                    s.add(Action(account_id=ACCOUNT, action_id=str(uuid.UUID(int=1000)),
                        binding_id="binding-" + ACCOUNT, fan_id=2000, kind="offer", anchor_message_id=1,
                        command_key="concurrent-new-row", request_hash="c" * 64, snapshot_json="{}", snapshot_hash="d" * 64,
                        status="cancelled", due_at=now, expires_at=now + timedelta(hours=1),
                        created_at=now + timedelta(seconds=1), updated_at=now))
            cursor = payload["nextCursor"]
            if cursor is None:
                break
        assert len(seen) == len(set(seen)) == 125
        with Session(database[1]) as s:
            assert s.scalar(select(func.count()).select_from(Action).where(Action.updated_at != now)) == 0
        assert browser.get(path, params={"limit": 0}, headers=header()).status_code == 400
        assert browser.get(path, params={"cursor": "bad"}, headers=header()).status_code == 400
    finally:
        store.close()


def scan_seed(store, account, *, busy=False):
    with Session(store.engine) as s, s.begin():
        from db.onlyfans_connection_models import OfProviderConnection
        connection = s.get(OfProviderConnection, account)
        now = utcnow()
        s.add(OfLedgerScan(account_id=account, binding_id=connection.binding_id,
            connection_revision=connection.revision, owner_id=connection.owner_id,
            window_start=now - timedelta(days=90), window_end=now - timedelta(minutes=5),
            page_offset=40, status="running" if busy else "held", lease_token="lease" if busy else None,
            lease_until=now + timedelta(minutes=1) if busy else None, next_poll_at=now + timedelta(minutes=5),
            last_error="unsupported_ledger_response", updated_at=now))


@pytest.mark.parametrize("busy", [False, True])
def test_ledger_retry_is_audited_replayable_and_preserves_frozen_work(database, monkeypatch, busy):
    store, account, _ = verified(database, monkeypatch)
    try:
        scan_seed(store, account, busy=busy)
        with Session(store.engine) as s:
            state = read_ledger(s, TENANT, account)
            scan = s.get(OfLedgerScan, account)
            frozen = (scan.window_start, scan.window_end, scan.page_offset, scan.status, scan.last_error)
        args = dict(command_key="a" * 64, actor=ACTOR, expected_revision=state["connectionRevision"],
            expected_fingerprint=state["scanFingerprint"])
        if busy:
            with pytest.raises(Refused, match="ledger busy"):
                retry_ledger(store, TENANT, account, **args)
            with Session(store.engine) as s:
                scan = s.get(OfLedgerScan, account)
                assert scan.status == "running" and scan.lease_token == "lease" and scan.page_offset == 40
                assert s.scalar(select(func.count()).select_from(LedgerRetryCommand)) == 0
            return
        result = retry_ledger(store, TENANT, account, **args)
        assert result == retry_ledger(store, TENANT, account, **args)
        assert result["outcome"] == "queued" and result["accepted"] is True
        assert "complete" not in result
        with Session(store.engine) as s:
            scan = s.get(OfLedgerScan, account)
            assert (scan.window_start, scan.window_end, scan.page_offset, scan.status, scan.last_error) == frozen
            assert scan.page_offset == 40 and scan.last_error == "unsupported_ledger_response"
            assert s.scalar(select(func.count()).select_from(LedgerRetryCommand)) == 1
            assert s.scalar(select(func.count()).select_from(OfLaunchCommand)) == 0
        with pytest.raises(Refused):
            retry_ledger(store, "other-tenant", account, **args)
        with pytest.raises(Refused, match="conflict"):
            retry_ledger(store, TENANT, account, **{**args, "expected_revision": 2})
        if not busy:
            with pytest.raises(Refused, match="state changed"):
                retry_ledger(store, TENANT, account, **{**args, "command_key": "b" * 64})
    finally:
        store.close()


def test_resolution_requires_verified_provider_message_identity_and_keeps_evidence(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    try:
        scan_seed(store, account)
        observation = parsed(transaction())
        assert record(store, account, observation) == "applied"
        state = ledger_status(store, TENANT, account)
        evidence_hash = state["evidence"][0]["evidenceHash"]
        result = resolve_ledger(store, TENANT, account, evidence_hash=evidence_hash,
            command_key="c" * 64, actor=ACTOR, reason="Investigate missing provider identity", expected_fingerprint=state["fingerprint"])
        assert result["resolved"] is False and result["blocker"] == "provider_message_identity_missing"
        with Session(store.engine) as s:
            assert s.scalar(select(Transaction.message_id)) is None
            assert s.scalar(select(func.count()).select_from(VaultSend)) == 0
            assert s.scalar(select(func.count()).select_from(OfLedgerEvidence)) == 1
    finally:
        store.close()


@pytest.mark.parametrize("legacy_evidence", [False, True])
@pytest.mark.parametrize("historical", [False, True])
def test_verified_resolution_after_missing_history_links_purchase_and_replay_is_exact(database, monkeypatch, legacy_evidence, historical):
    from ownership import owned_media_in_session
    from onlyfans_runtime.launch_operations_models import OfLedgerResolution
    store, account, _ = verified(database, monkeypatch)
    try:
        scan_seed(store, account)
        observation = parsed(transaction(messageId=123, amount="10.01", net="8.01", fee="2.00"))
        if historical:
            observation["occurred_at"] = (utcnow() - timedelta(days=30)).isoformat()
        if legacy_evidence:
            from onlyfans_runtime.launch_operations import digest
            with Session(store.engine) as s, s.begin():
                s.add(OfLedgerEvidence(account_id=account, evidence_hash=digest(observation),
                    provider_transaction_id=observation["provider_transaction_id"], fan_id=101,
                    outcome="conflict", observation_json=canonical(observation), recorded_at=utcnow()))
        else:
            assert record(store, account, observation) == "applied", "verified money grants no media without history"
        assert record(store, account, observation) == "duplicate", "observation dedup remains intact"
        with Session(store.engine) as s:
            tx = s.scalar(select(Transaction))
            assert (tx is None) is legacy_evidence
            if tx is not None:
                assert tx.amount_cents == 1001 and tx.message_id is None
                assert s.get(Fan, (account, 101)).lifetime_spend_cents == 1001
            assert owned_media_in_session(s, account, 101) == set()
        missing = ledger_status(store, TENANT, account)
        missing_args = dict(evidence_hash=missing["evidence"][0]["evidenceHash"], command_key="0" * 64,
            actor=ACTOR, reason="Recover verified financial evidence while history is missing",
            expected_fingerprint=missing["fingerprint"])
        unresolved = resolve_ledger(store, TENANT, account, **missing_args)
        assert unresolved["resolved"] is False and unresolved["blocker"] == "canonical_paid_message_conflict"
        assert resolve_ledger(store, TENANT, account, **missing_args) == unresolved
        with Session(store.engine) as s:
            tx = s.scalar(select(Transaction))
            assert tx.amount_cents == 1001 and tx.message_id is None
            assert s.get(Fan, (account, 101)).lifetime_spend_cents == 1001
            assert owned_media_in_session(s, account, 101) == set()
            assert s.scalar(select(func.count()).select_from(OfLedgerEvidence)) == 1
        with Session(store.engine) as s, s.begin():
            if s.get(Fan, (account, 101)) is None:
                s.add(Fan(account_id=account, fan_id=101))
            s.flush()
            s.add(Message(account_id=account, fan_id=101, message_id=123,
                direction="out", body="Retained original bundle", media_ids="[12,11]", media_count=2,
                price_cents=1001, created_at=datetime.fromisoformat(observation["occurred_at"]) - timedelta(hours=1)))
        invalidations = []
        monkeypatch.setattr("onlyfans_runtime.launch_engagement.service.invalidate",
            lambda *args, **kwargs: invalidations.append(kwargs))
        state = ledger_status(store, TENANT, account)
        args = dict(evidence_hash=state["evidence"][0]["evidenceHash"], command_key="1" * 64,
            actor=ACTOR, reason="Exact provider identity is now supported by retained history", expected_fingerprint=state["fingerprint"])
        result = resolve_ledger(store, TENANT, account, **args)
        assert result["resolved"] is True
        assert resolve_ledger(store, TENANT, account, **args) == result
        assert len(invalidations) == (0 if historical else 1)
        after = ledger_status(store, TENANT, account)
        assert after["conflictingEvidence"] == 0 and after["evidence"][0]["resolved"] is True
        assert after["commerceHold"] is True, "resolving evidence does not complete the still-held scan"
        with Session(store.engine) as s:
            assert owned_media_in_session(s, account, 101) == {11, 12}
            tx = s.scalar(select(Transaction))
            assert tx.amount_cents == 1001 and tx.message_id == 123
            assert (tx.net_cents, tx.fee_cents) == (801, 200)
            assert s.get(Fan, (account, 101)).lifetime_spend_cents == 1001
            assert s.scalar(select(OfLedgerEvidence.outcome)) == ("conflict" if legacy_evidence else "applied"), "original evidence stays immutable"
            assert s.scalar(select(func.count()).select_from(OfLedgerResolution)) == 1
            assert s.scalar(select(func.count()).select_from(OfLaunchCommand)) == 2
            assert s.scalar(select(func.count()).select_from(Transaction)) == 1
    finally:
        store.close()


def test_resolution_cannot_change_message_identity_retained_before_history(database, monkeypatch):
    from ownership import owned_media_in_session
    store, account, _ = verified(database, monkeypatch)
    try:
        scan_seed(store, account)
        observed = parsed(transaction(messageId=123))
        assert record(store, account, observed) == "applied"
        changed = {**observed, "message_id": 124}
        assert record(store, account, changed) == "conflict"
        with Session(store.engine) as s, s.begin():
            s.add(Message(account_id=account, fan_id=101, message_id=124,
                direction="out", body="Different paid message", media_ids="[12]", media_count=1,
                price_cents=1000, created_at=utcnow() - timedelta(hours=1)))
        state = ledger_status(store, TENANT, account)
        from onlyfans_runtime.launch_operations import digest
        result = resolve_ledger(store, TENANT, account, evidence_hash=digest(changed),
            command_key="3" * 64, actor=ACTOR, reason="Check conflicting retained identity",
            expected_fingerprint=state["fingerprint"])
        assert result["resolved"] is False and result["blocker"] == "transaction_identity_conflict"
        with Session(store.engine) as s:
            assert owned_media_in_session(s, account, 101) == set()
            assert s.scalar(select(Transaction.message_id)) is None
            assert s.get(Fan, (account, 101)).lifetime_spend_cents == 1000
            assert s.scalar(select(func.count()).select_from(Transaction)) == 1
    finally:
        store.close()


def test_ledger_freshness_follows_current_connection_revision_without_erasing_window(database, monkeypatch):
    from db.onlyfans_connection_models import OfProviderConnection
    store, account, _ = verified(database, monkeypatch)
    try:
        scan_seed(store, account)
        with Session(store.engine) as s, s.begin():
            scan = s.get(OfLedgerScan, account)
            scan.status, scan.last_error, scan.last_complete_at = "idle", None, utcnow()
            scan.coverage_json=_coverage_receipt(scan)
        before = ledger_status(store, TENANT, account)
        assert before["fresh"] is True
        with Session(store.engine) as s, s.begin():
            s.get(OfProviderConnection, account).revision += 1
        after = ledger_status(store, TENANT, account)
        assert after["fresh"] is False and after["bindingCurrent"] is True
        assert (after["windowStart"], after["windowEnd"], after["pageOffset"]) == (
            before["windowStart"], before["windowEnd"], before["pageOffset"])
    finally:
        store.close()


def test_prior_binding_ledger_evidence_is_not_exposed_or_resolved(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    try:
        scan_seed(store, account)
        observed = parsed(transaction(messageId=123))
        from onlyfans_runtime.launch_operations import digest
        fingerprint = digest(observed)
        with Session(store.engine) as s, s.begin():
            binding = s.get(OfCreatorBinding, s.get(OfRuntime, account).binding_id)
            s.add(OfLedgerEvidence(account_id=account, evidence_hash=fingerprint,
                provider_transaction_id=observed["provider_transaction_id"], fan_id=observed["fan_id"],
                outcome="conflict", observation_json=canonical(observed),
                recorded_at=binding.created_at - timedelta(seconds=1)))
        state = ledger_status(store, TENANT, account)
        assert state["evidence"] == [] and state["conflictingEvidence"] == 0
        with pytest.raises(Refused, match="evidence unavailable"):
            resolve_ledger(store, TENANT, account, evidence_hash=fingerprint, command_key="9" * 64,
                actor=ACTOR, reason="Scope check", expected_fingerprint=state["fingerprint"])
    finally:
        store.close()


@pytest.mark.parametrize("wire_started", [False, True])
def test_stop_races_final_dispatch_without_releasing_or_replaying_uncertain_attempt(rig, wire_started):
    from db.onlyfans_delivery_models import OfTextIntent
    service, engine, item, now = rig
    schedule(rig)
    proposal = service.due(TENANT, ACCOUNT)
    authority = TextAuthority(service.store)
    intent = native_prepare(authority, engine, item, proposal)
    grant = authority.admit(TENANT, ACCOUNT, intent)
    assert grant.intent_id == intent
    if wire_started:
        assert authority.consume_dispatch(TENANT, grant, transport_generation=grant.policy_revision) is not None
    with Session(engine) as s, s.begin():
        before = account_state(s, service.store._runtime(s, TENANT, ACCOUNT))
    ack = control_account(service.store, TENANT, ACCOUNT, action="emergency_stop", command_key="2" * 64,
        actor=ACTOR, reason="Stop at dispatch boundary", expected_fingerprint=before["fingerprint"])
    assert ack["unresolvedOutgoing"] == 1
    if wire_started:
        authority.mark_unknown(TENANT, ACCOUNT, intent, grant.token)
        with Session(engine) as s:
            row = s.get(OfTextIntent, (ACCOUNT, intent))
            assert row.status == "unknown" and row.wire_started_at is not None
            assert s.get(Action, (ACCOUNT, proposal["action_id"])).cancel_requested_at is not None
    else:
        assert authority.consume_dispatch(TENANT, grant, transport_generation=grant.policy_revision) is None
        with Session(engine) as s:
            assert s.get(OfTextIntent, (ACCOUNT, intent)).status in {"refused", "cancelled"}
    with Session(engine) as s:
        assert s.get(OfRuntime, ACCOUNT).enabled is False
        assert s.scalar(select(func.count()).select_from(Message).where(Message.message_id == 9001)) == 0


def test_stop_is_recorded_readback_authoritative_and_resume_cannot_release_stale_work(database):
    store = seed(*database)
    try:
        with Session(store.engine) as s, s.begin():
            before = account_state(s, store._runtime(s, TENANT, ACCOUNT))
        args = dict(action="emergency_stop", command_key="d" * 64, actor=ACTOR,
            reason="Controlled synthetic emergency stop", expected_fingerprint=before["fingerprint"])
        ack = control_account(store, TENANT, ACCOUNT, **args)
        assert ack["recorded"] is True and ack["enabled"] is False and ack["oldWorkResumed"] is False
        assert control_account(store, TENANT, ACCOUNT, **args) == ack
        with Session(store.engine) as s, s.begin():
            after = account_state(s, store._runtime(s, TENANT, ACCOUNT))
            assert after["enabled"] is False and after["revision"] == 1
        with pytest.raises(Refused, match="prerequisites"):
            control_account(store, TENANT, ACCOUNT, action="resume", command_key="e" * 64,
                actor=ACTOR, reason="Explicit synthetic resume", expected_fingerprint=after["fingerprint"])
        with Session(store.engine) as s:
            assert s.get(OfRuntime, ACCOUNT).enabled is False
    finally:
        store.close()


@pytest.mark.parametrize("pending_draft", [False, True])
def test_explicit_resume_uses_actual_ready_owners_and_leaves_old_work_blocked(database, monkeypatch, pending_draft):
    from test_onlyfans_media_pg import shelf
    from test_onlyfans_launch_personality_workflow import MODEL, ACK
    from db.models import CatalogItem, OfConversationState
    from db.onlyfans_delivery_models import OfDeliveryPolicy
    from db.onlyfans_models import OfWork
    from onlyfans_runtime.launch_operations_models import OfLaunchWorkerHealth
    from onlyfans_runtime.launch_personality.workflow import PersonalityWorkflow
    store, account, _ = verified(database, monkeypatch)
    try:
        now = utcnow()
        with Session(store.engine) as s, s.begin():
            runtime = s.get(OfRuntime, account)
            binding = s.get(OfCreatorBinding, runtime.binding_id)
            binding_id, creator_id = binding.id, binding.creator_id
            runtime.last_sync_at, runtime.recovery_needed = now, False
            s.add(Fan(account_id=account, fan_id=101))
            s.flush()
            s.add(OfConversationState(account_id=account, fan_id=101, mode="automated", revision=0,
                takeover_state="none", requires_attention=False))
            s.add(OfWork(account_id=account, fan_id=101, generation=1, status="blocked",
                first_input_at=now, last_input_at=now, due_at=now, target_message_id=1, control_revision=0))
            s.add(OfDeliveryPolicy(account_id=account, enabled=True, media_enabled=True,
                transport_kind="native", limit_units=10, used_units=0, queued_count=0, throttle_seconds=1))
            s.add(OfLaunchWorkerHealth(account_id=account, binding_id=binding_id, observed_at=now,
                capabilities_json=canonical({"history_recovery": True, "history_memory": True,
                    "engagement": False, "connection_revision": 1})))
        item = shelf(store.engine, account=account)
        with Session(store.engine) as s, s.begin():
            s.get(CatalogItem, item).pricing_mode = "fixed"
        scan_seed(store, account)
        with Session(store.engine) as s, s.begin():
            scan = s.get(OfLedgerScan, account)
            scan.status, scan.last_error, scan.last_complete_at, scan.window_end = "idle", None, now, now
            scan.coverage_json=_coverage_receipt(scan)
        credentials = lambda s, a, b: {"accountId": a, "modelId": MODEL, "configured": True,
            "expiresAt": (now + timedelta(hours=1)).isoformat() + "Z"}
        workflow = PersonalityWorkflow(store, credential_status_reader=credentials)
        workflow.import_draft(TENANT, account,
            profile={"accountId": account, "bindingId": binding_id, "creatorId": creator_id,
                "sourceId": "synthetic-profile", "fields": {}},
            messages=[{"accountId": account, "bindingId": binding_id, "sourceId": "synthetic-source",
                "direction": "out", "body": "hey, how was your afternoon?", "author": "unknown",
                "fanKey": "synthetic-fan", "occurredAt": now.isoformat() + "Z"}],
            expected_revision=0, command_key="3" * 64, actor=ACTOR)
        workflow.edit(TENANT, account, review={"persona": "Warm, concise and conversational. Ask gentle questions.",
            "replyModelOverride": MODEL}, expected_revision=1, command_key="4" * 64, actor=ACTOR)
        workflow.approve(TENANT, account, acknowledgements=ACK, expected_revision=2, command_key="5" * 64, actor=ACTOR)
        if pending_draft:
            draft = workflow.edit(TENANT, account, review={"persona": "A new gentle conversational tone awaiting review."},
                expected_revision=3, command_key="7" * 64, actor=ACTOR)
            assert draft["state"] == "draft" and workflow.get(TENANT, account)["state"] == "draft"
            with Session(store.engine) as s:
                assert s.get(AccountAiConfig, account).persona == "Warm, concise and conversational. Ask gentle questions."
        readiness = launch_readiness(store, TENANT, account, credential_reader=credentials)
        assert readiness["readyToEnable"] is True, readiness["checks"]
        assert next(c for c in readiness["checks"] if c["key"] == "personality")["status"] == "ready"
        assert readiness["autonomy"]["accountEnabled"] is False
        from db.onlyfans_creator_facts_models import OfCreatorFactsSource
        with Session(store.engine) as s, s.begin():
            s.add(OfCreatorFactsSource(account_id=account, binding_id=binding_id,
                source_json="{}", source_hash="a" * 64, overrides_json="{}",
                checked_at=now - timedelta(days=365)))
        retained = launch_readiness(store, TENANT, account, credential_reader=credentials)
        # Approved facts deliberately have no age expiry (the retained PR #67
        # repair). Time alone must not invalidate the saved personality review.
        assert next(c for c in retained["checks"] if c["key"] == "personality")["status"] == "ready"
        assert retained["readyToEnable"] is True
        with Session(store.engine) as s, s.begin():
            s.get(OfCreatorFactsSource, account).checked_at = utcnow()
        readiness = launch_readiness(store, TENANT, account, credential_reader=credentials)
        assert readiness["readyToEnable"] is True
        result = control_account(store, TENANT, account, action="resume", command_key="6" * 64, actor=ACTOR,
            reason="Explicit synthetic resume after full readiness", expected_fingerprint=readiness["accountControl"]["fingerprint"],
            credential_reader=credentials)
        assert result["enabled"] is True and result["oldWorkResumed"] is False
        with Session(store.engine) as s:
            assert s.get(OfWork, (account, 101)).status == "blocked"
            assert s.get(OfRuntime, account).enabled is True
        # Actual saved configuration changes invalidate the old approval even
        # though a draft alone kept the prior reviewed configuration effective.
        from onlyfans_runtime.configuration import get_configuration, update_configuration
        configuration = get_configuration(store, TENANT, account)
        update_configuration(store, TENANT, account, patch={"dailyCostCapCents": 101},
            expected_revision=configuration["configurationRevision"], expected_fingerprint=configuration["fingerprint"],
            command_key="8" * 64, actor=ACTOR)
        changed = launch_readiness(store, TENANT, account, credential_reader=credentials)
        assert changed["readyToEnable"] is False
        assert next(c for c in changed["checks"] if c["key"] == "personality")["status"] == "blocked"
    finally:
        store.close()


def test_readiness_is_real_evidence_and_missing_credentials_hold_launch(database, monkeypatch):
    store = seed(*database)
    try:
        with Session(store.engine) as s, s.begin():
            s.add(AccountAiConfig(account_id=ACCOUNT))
        result = launch_readiness(store, TENANT, ACCOUNT)
        checks = {check["key"]: check for check in result["checks"]}
        assert result["readyToEnable"] is False and checks["schema"]["status"] == "ready"
        assert checks["credential"]["blocker"] == "credential_missing"
        assert checks["worker"]["status"] == "unknown" and checks["ledger"]["status"] == "blocked"
        assert result["coverage"]["providerTotalMessages"] is None
        browser = client(store, monkeypatch)
        path = f"/onlyfans/api/v1/accounts/{ACCOUNT}/readiness"
        assert browser.get(path, headers=header("read")).status_code == 200
        assert browser.get(path, headers=header("control")).status_code == 401
        assert browser.get(path.replace(ACCOUNT, "other-account"), headers=header("read")).status_code == 404
    finally:
        store.close()


@pytest.mark.parametrize("spent,expected", [(9999, "ready"), (10000, "blocked")])
def test_readiness_uses_native_hundredth_cent_spend_units(database, spent, expected):
    from db.models import GrokDailyCost
    from test_onlyfans_launch_personality_workflow import MODEL
    store = seed(*database)
    try:
        with Session(store.engine) as s, s.begin():
            s.add(AccountAiConfig(account_id=ACCOUNT, model=MODEL, daily_cost_cap_cents=100))
            s.add(GrokDailyCost(day=utcnow().date().isoformat(), account_id=ACCOUNT,
                provider="grok", cost_cents=spent, call_count=1))
            s.add(GrokDailyCost(day=utcnow().date().isoformat(), account_id=ACCOUNT,
                provider="deepseek", cost_cents=999999, call_count=1))
        checks = {check["key"]: check for check in launch_readiness(store, TENANT, ACCOUNT)["checks"]}
        assert checks["budget"]["status"] == expected
        assert checks["budget"]["blocker"] == (None if expected == "ready" else "budget_exhausted")
    finally:
        store.close()


def test_control_purpose_and_readonly_agency_cannot_mutate(database, monkeypatch):
    store = seed(*database)
    try:
        browser = client(store, monkeypatch)
        path = f"/onlyfans/control/v1/accounts/{ACCOUNT}/control"
        with Session(store.engine) as s, s.begin():
            fingerprint = account_state(s, store._runtime(s, TENANT, ACCOUNT))["fingerprint"]
        payload = {"action": "pause", "commandKey": "f" * 64, "actor": ACTOR,
            "reason": "Synthetic pause", "expectedFingerprint": fingerprint}
        assert browser.post(path, json=payload, headers=header("configuration")).status_code == 401
        agency = {"creatorIds": ["00000000-0000-0000-0000-000000000001"],
            "actor": "agency-user:00000000-0000-0000-0000-000000000002:00000000-0000-0000-0000-000000000003", "canManage": False}
        response = browser.post(path, json={**payload, "actor": agency["actor"]},
            headers={**header("control"), "x-onlyfans-agency-scope": json.dumps(agency)})
        assert response.status_code == 403
        with Session(store.engine) as s:
            assert s.get(OfRuntime, ACCOUNT).enabled is True
            assert s.scalar(select(func.count()).select_from(OfLaunchCommand)) == 0
    finally:
        store.close()


@pytest.mark.parametrize("projection", ["launch_readiness", "ledger_status"])
def test_read_projection_snapshot_fences_concurrent_tenant_rebinding(database, monkeypatch, projection):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    import onlyfans_runtime.launch_operations as operations
    store = seed(*database)
    original = operations.read_scope
    checked = []
    def observe(owner, session, tenant, account):
        runtime = original(owner, session, tenant, account)
        assert session.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        assert session.scalar(text("SHOW transaction_read_only")) == "on"
        old_binding = runtime.binding_id
        with Session(store.engine) as other, other.begin():
            other.get(OfCreatorBinding, old_binding).revoked_at = utcnow()
            other.flush()
            other.add(OfCreatorBinding(id="snapshot-new-owner", tenant_id="another-tenant",
                creator_id="another-creator", creator_name="New synthetic owner",
                account_id=account, handle="synthetic", channel="onlyfans"))
            other.flush()
            other.get(OfRuntime, account).binding_id = "snapshot-new-owner"
            other.add(Message(account_id=account, fan_id=FAN, message_id=999,
                direction="in", body="New owner's private evidence", created_at=utcnow()))
        assert runtime.binding_id == old_binding
        assert session.scalar(select(func.count()).select_from(Message).where(Message.account_id == account)) == 0
        with pytest.raises(DBAPIError):
            with session.begin_nested():
                session.execute(text("UPDATE of_runtime SET enabled=false WHERE account_id=:account"), {"account": account})
        checked.append(True)
        return runtime
    monkeypatch.setattr(operations, "read_scope", observe)
    try:
        from onlyfans_runtime.launch_personality_credentials import personality_credential_status
        kwargs = {"credential_reader": personality_credential_status} if projection == "launch_readiness" else {}
        getattr(operations, projection)(store, TENANT, ACCOUNT, **kwargs)
        assert checked == [True]
        monkeypatch.setattr(operations, "read_scope", original)
        with pytest.raises(Refused):
            getattr(operations, projection)(store, TENANT, ACCOUNT)
    finally:
        store.close()


def test_slow_stored_count_is_explicitly_unavailable_without_losing_readiness(database):
    from sqlalchemy import event as sql_event, text
    store = seed(*database)
    injected = []
    def delay_count(conn, cursor, statement, parameters, context, executemany):
        if not injected and "count(*)" in statement and "FROM messages" in statement:
            injected.append(True)
            return "SELECT count(*) FROM pg_sleep(3)", {}
        return statement, parameters
    sql_event.listen(store.engine, "before_cursor_execute", delay_count, retval=True)
    try:
        result = launch_readiness(store, TENANT, ACCOUNT)
        assert injected == [True]
        assert result["coverage"]["storedMessages"] is None
        assert result["coverage"]["storedCountStatus"] == "unavailable"
        assert result["coverage"]["processedMessages"] is None
        assert result["coverage"]["memorySample"] == {"inspectedMessages": 0, "processedCurrentMessages": 0, "complete": False}
        assert next(c for c in result["checks"] if c["key"] == "memory")["status"] == "unknown"
        assert result["readyToEnable"] is False
        with Session(store.engine) as session:
            assert session.scalar(text("SHOW statement_timeout")) != "1500ms"
    finally:
        sql_event.remove(store.engine, "before_cursor_execute", delay_count)
        store.close()
