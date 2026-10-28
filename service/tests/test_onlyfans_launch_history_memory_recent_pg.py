"""Recent/archive cutover on disposable PostgreSQL; only synthetic models."""
from dataclasses import replace
from datetime import timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, seed, event, due
from db.models import AccountAiConfig, Message
from db.onlyfans_models import OfRecovery, OfRuntime, OfWork
from db.onlyfans_delivery_models import OfNativePlan, OfTextIntent
from onlyfans_runtime.launch_history_memory.recent import request_before, commit_recent_page
from onlyfans_runtime.launch_history_memory.recovery import RecoveryBase, HistoryRecovery, OfHistoryCursor
from onlyfans_runtime.native_planning import NativeTextPlanner, SyntheticNativeModel
from onlyfans_runtime.store import Event, Refused, Store, utcnow
from onlyfans_runtime.text_delivery import TextAuthority

TENANT, ACCOUNT, FAN = "tenant-a", "acc-a", 101


def head():
    stamp = (utcnow() - timedelta(minutes=10)).replace(tzinfo=timezone.utc)
    return [Event(FAN, mid, (stamp + timedelta(seconds=mid)).isoformat(), "in",
                  f"Synthetic historical {mid}") for mid in range(200, 100, -1)]


@pytest.fixture
def initial(database):
    url, engine = database
    store = seed(url, engine)
    store.begin_scan(TENANT, ACCOUNT)
    scan = store.claim_scan(TENANT, ACCOUNT)
    store.scan_page(TENANT, scan, [FAN], next_cursor=None)
    try:
        yield store, engine, store.claim_recovery(TENANT, ACCOUNT)
    finally:
        store.close()


def test_initial_cutover_is_silent_and_live_model_progresses_with_archive_pending(initial, monkeypatch):
    store, engine, claim = initial
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    monkeypatch.setenv("DATABASE_URL", engine.url.render_as_string(hide_password=False))
    assert request_before(store, TENANT, claim) is None
    assert commit_recent_page(store, TENANT, claim, head(), has_more=True) == "complete"
    with Session(engine) as s, s.begin():
        assert s.scalar(select(func.count()).select_from(OfWork)) == 0
        assert s.get(OfRecovery, (ACCOUNT, FAN)).high_water == 200
        cursor = s.get(OfHistoryCursor, (ACCOUNT, FAN))
        assert cursor.before_id == 101 and cursor.status == "needed"
        assert not s.get(OfRuntime, ACCOUNT).recovery_needed
        s.add(AccountAiConfig(account_id=ACCOUNT, model="grok-4-1-fast-non-reasoning",
            persona="Synthetic Creator enjoys hiking and coffee.",
            persona_facts_json='{"name":"Synthetic Creator","location":"Brisbane"}'))
    authority = TextAuthority(store)
    authority.configure(TENANT, ACCOUNT, expected_revision=0, enabled=True, limit_units=20,
                        throttle_seconds=1, actor="synthetic-operator", reason="Offline test")
    store.record(TENANT, ACCOUNT, event(mid=201, body="A new message after history import"))
    store.drain(TENANT, ACCOUNT)
    due(engine)
    live = store.claim(TENANT)
    assert live is not None and live.target_message_id == 201
    assert HistoryRecovery(store).claim(TENANT, ACCOUNT) is None  # Live claim gets priority.
    model = SyntheticNativeModel("Thanks for your new message.")
    plan_id = NativeTextPlanner(store).prepare(TENANT, live, model)
    with Session(engine) as s:
        assert s.get(OfNativePlan, (ACCOUNT, plan_id)).status == "ready"
        assert s.scalar(select(func.count()).select_from(OfTextIntent)) == 0
        assert s.get(OfHistoryCursor, (ACCOUNT, FAN)).status == "needed"
    assert len(model.calls) == 1  # Generation proof only; no transport invoked.


def test_cutover_restarts_from_newest_and_preserves_existing_archive_cursor(initial):
    store, engine, claim = initial
    with Session(engine) as s, s.begin():
        row = s.get(OfRecovery, (ACCOUNT, FAN))
        row.before_id, row.upper_bound = 40, 160
    claim = replace(claim, before_id=40, upper_bound=160)
    recovery = HistoryRecovery(store)
    recovery.request_fan(TENANT, ACCOUNT, FAN, before_id=20)
    assert request_before(store, TENANT, claim) is None
    assert commit_recent_page(store, TENANT, claim, head(), has_more=True) == "complete"
    store.close()
    reopened = Store(engine.url.render_as_string(hide_password=False))
    try:
        cursor = HistoryRecovery(reopened).claim(TENANT, ACCOUNT)
        assert cursor.before_id == 20
        with pytest.raises(Refused, match="stale"):
            commit_recent_page(reopened, TENANT, claim, head(), has_more=True)
    finally:
        reopened.close()


def test_existing_high_water_catchup_is_never_truncated(initial):
    store, engine, claim = initial
    with Session(engine) as s, s.begin():
        s.get(OfRecovery, (ACCOUNT, FAN)).high_water = 50
    claim = replace(claim, high_water=50)
    assert commit_recent_page(store, TENANT, claim, head(), has_more=True) == "continue"
    with Session(engine) as s:
        assert s.get(OfRuntime, ACCOUNT).recovery_needed
        assert s.get(OfHistoryCursor, (ACCOUNT, FAN)) is None
        assert s.get(OfRecovery, (ACCOUNT, FAN)).high_water == 50
    next_claim = store.claim_recovery(TENANT, ACCOUNT)
    assert request_before(store, TENANT, next_claim) == 101


@pytest.mark.parametrize("case", ["order", "fan", "future", "empty", "conflict"])
def test_invalid_head_never_commits_partial_cutover(initial, case):
    store, engine, claim = initial
    rows = head()
    if case == "order": rows.reverse()
    if case == "fan": rows[-1] = replace(rows[-1], fan_id=102)
    if case == "future": rows[-1] = replace(rows[-1], created_at=(utcnow() + timedelta(days=1)).replace(tzinfo=timezone.utc).isoformat())
    if case == "empty": rows = []
    if case == "conflict":
        store.record(TENANT, ACCOUNT, replace(rows[-1], body="different"), source="import")
        store.drain(TENANT, ACCOUNT)
    with pytest.raises(Refused):
        commit_recent_page(store, TENANT, claim, rows, has_more=True)
    with Session(engine) as s:
        assert s.get(OfRuntime, ACCOUNT).recovery_needed
        assert s.get(OfRecovery, (ACCOUNT, FAN)).high_water == 0
        assert s.get(OfHistoryCursor, (ACCOUNT, FAN)) is None
        assert s.scalar(select(func.count()).select_from(Message)) == (1 if case == "conflict" else 0)


def test_other_recent_recovery_still_holds_account(initial):
    store, engine, claim = initial
    store.need_recovery(TENANT, ACCOUNT, 102)
    commit_recent_page(store, TENANT, claim, head(), has_more=True)
    with Session(engine) as s:
        assert s.get(OfRuntime, ACCOUNT).recovery_needed


def test_short_pages_accumulate_a_hundred_recent_messages_across_restart(initial):
    store, engine, claim = initial
    rows = head()
    for start in range(0, 100, 20):
        assert request_before(store, TENANT, claim) == (None if start == 0 else rows[start - 1].message_id)
        result = commit_recent_page(store, TENANT, claim, rows[start:start + 20], has_more=True)
        assert result == ("complete" if start == 80 else "continue")
        if start < 80:
            store.close()
            store = Store(engine.url.render_as_string(hide_password=False))
            claim = store.claim_recovery(TENANT, ACCOUNT)
            with Session(engine) as s:
                assert s.get(OfRuntime, ACCOUNT).recovery_needed
    with Session(engine) as s:
        assert s.get(OfRecovery, (ACCOUNT, FAN)).high_water == 200
        assert s.get(OfHistoryCursor, (ACCOUNT, FAN)).before_id == 101
    store.close()


def test_completed_empty_history_does_not_silence_future_first_message(initial):
    store, engine, claim = initial
    assert commit_recent_page(store, TENANT, claim, [], has_more=False) == "complete"
    store.need_recovery(TENANT, ACCOUNT, FAN)
    claim = store.claim_recovery(TENANT, ACCOUNT)
    assert request_before(store, TENANT, claim) is None
    assert commit_recent_page(store, TENANT, claim, [event(mid=1)], has_more=False) == "complete"
    store.drain(TENANT, ACCOUNT)
    with Session(engine) as s:
        assert s.get(OfWork, (ACCOUNT, FAN)).target_message_id == 1


# --- Routine freshness polls, scoped freshness and archive-independent recent bootstrap ----------------------
from onlyfans_runtime.store import fan_history_current
from onlyfans_runtime.text_delivery import MAX_SYNC_AGE_SECONDS


def fresh_fan(store, engine, monkeypatch):
    """Recent recovery complete for the approved fan; account proof fresh but older than the poll interval."""
    monkeypatch.setenv("ONLYFANS_FAN_CURRENT_OVERRIDE", f"{ACCOUNT}:{FAN}")
    claim = store.claim_recovery(TENANT, ACCOUNT)
    assert commit_recent_page(store, TENANT, claim, head(), has_more=True) == "complete"
    with Session(engine) as s, s.begin():
        runtime = s.get(OfRuntime, ACCOUNT)
        assert not runtime.recovery_needed and runtime.last_sync_at is not None
        runtime.last_sync_at = utcnow() - timedelta(seconds=30)
        return runtime.last_sync_at


def fan_current(engine):
    with Session(engine) as s:
        return fan_history_current(s, ACCOUNT, FAN)


def test_routine_poll_keeps_valid_proof_and_completes_without_a_reset(database, monkeypatch):
    url, engine = database
    store = seed(url, engine)
    store.begin_scan(TENANT, ACCOUNT)
    scan = store.claim_scan(TENANT, ACCOUNT)
    store.scan_page(TENANT, scan, [FAN], next_cursor=None)
    try:
        synced = fresh_fan(store, engine, monkeypatch)
        authority = TextAuthority(store)
        authority.configure(TENANT, ACCOUNT, expected_revision=0, enabled=True, limit_units=20,
                            throttle_seconds=1, actor="synthetic-operator", reason="Offline test")
        assert fan_current(engine)
        assert store.request_poll(TENANT, ACCOUNT, min_interval_seconds=5) is True
        assert store.request_poll(TENANT, ACCOUNT, min_interval_seconds=5) is False  # one probe in flight, no re-reset
        with Session(engine) as s:
            runtime = s.get(OfRuntime, ACCOUNT)
            assert runtime.poll_pending and not runtime.recovery_needed and runtime.last_sync_at == synced
            assert s.get(OfRecovery, (ACCOUNT, FAN)).status == "needed"  # the probe still re-reads the fan
            # Valid proof survives the queued read: account-wide, with no override either.
            monkeypatch.delenv("ONLYFANS_FAN_CURRENT_OVERRIDE")
            from db.models import Fan, OfConversationState
            from db.onlyfans_delivery_models import OfDeliveryPolicy
            assert authority._eligibility(s, runtime, s.get(OfDeliveryPolicy, ACCOUNT),
                s.get(OfConversationState, (ACCOUNT, FAN)), s.get(Fan, (ACCOUNT, FAN)), "automated", utcnow()) is None
            monkeypatch.setenv("ONLYFANS_FAN_CURRENT_OVERRIDE", f"{ACCOUNT}:{FAN}")
            assert fan_history_current(s, ACCOUNT, FAN)
        # The probe completes: proof is refreshed, the marker clears, and nothing was stranded.
        scan = store.claim_scan(TENANT, ACCOUNT)
        store.scan_page(TENANT, scan, [FAN], next_cursor=None)
        claim = store.claim_recovery(TENANT, ACCOUNT)
        assert store.recovery_page(TENANT, claim, [], has_more=False) == "complete"
        with Session(engine) as s:
            runtime = s.get(OfRuntime, ACCOUNT)
            assert not runtime.poll_pending and not runtime.recovery_needed and runtime.last_sync_at > synced
            assert s.get(OfRecovery, (ACCOUNT, FAN)).status == "complete"
    finally:
        store.close()


def test_real_disconnect_and_expired_freshness_still_require_reconciliation(database, monkeypatch):
    url, engine = database
    store = seed(url, engine)
    store.begin_scan(TENANT, ACCOUNT)
    scan = store.claim_scan(TENANT, ACCOUNT)
    store.scan_page(TENANT, scan, [FAN], next_cursor=None)
    try:
        fresh_fan(store, engine, monkeypatch)
        authority = TextAuthority(store)
        authority.configure(TENANT, ACCOUNT, expected_revision=0, enabled=True, limit_units=20,
                            throttle_seconds=1, actor="synthetic-operator", reason="Offline test")
        # Expired fan proof: a historical complete flag alone is not enough.
        with Session(engine) as s, s.begin():
            s.get(OfRecovery, (ACCOUNT, FAN)).last_complete_at = utcnow() - timedelta(seconds=MAX_SYNC_AGE_SECONDS + 60)
        assert not fan_current(engine)
        with Session(engine) as s, s.begin():
            s.get(OfRecovery, (ACCOUNT, FAN)).last_complete_at = utcnow()
        assert fan_current(engine)
        # Actual disconnect: begin_scan records the gap and the approved fan is no longer current.
        store.begin_scan(TENANT, ACCOUNT)
        with Session(engine) as s:
            assert s.get(OfRuntime, ACCOUNT).recovery_needed
        assert not fan_current(engine)
        # An unrelated fan is never current through the scoped override.
        with Session(engine) as s:
            assert not fan_history_current(s, ACCOUNT, FAN + 1)
        # Aged account proof (no override) is refused as history_not_current.
        monkeypatch.delenv("ONLYFANS_FAN_CURRENT_OVERRIDE")
        with Session(engine) as s, s.begin():
            runtime = s.get(OfRuntime, ACCOUNT)
            runtime.recovery_needed, runtime.last_sync_at = False, utcnow() - timedelta(seconds=MAX_SYNC_AGE_SECONDS + 60)
        with Session(engine) as s:
            from db.models import Fan, OfConversationState
            from db.onlyfans_delivery_models import OfDeliveryPolicy
            assert authority._eligibility(s, s.get(OfRuntime, ACCOUNT), s.get(OfDeliveryPolicy, ACCOUNT),
                s.get(OfConversationState, (ACCOUNT, FAN)), s.get(Fan, (ACCOUNT, FAN)), "automated", utcnow()) == "history_not_current"
    finally:
        store.close()


def test_recent_bootstrap_is_independent_of_the_archive_flag(database, monkeypatch):
    from onlyfans_worker_fixture import ACCOUNT as WACCOUNT
    from onlyfans_native_protocol_fixture import OWNER, native_message, native_peer
    from onlyfans_runtime.native_connector import NativeLoopbackConnector, from_native_server
    from onlyfans_runtime.native_worker import NativeWorker, WorkerAccount
    from onlyfans_runtime.launch_history_memory.recent import RecentBootstrap
    from onlyfans_runtime.launch_history_memory.recovery import OfHistoryScan
    url, engine = database
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    monkeypatch.setenv("DATABASE_URL", url)
    store = seed(url, engine, account=WACCOUNT)
    try:
        store.begin_scan("tenant-a", WACCOUNT)
        cutovers = []
        import onlyfans_runtime.worker as worker_module
        real = worker_module.read_step
        def spy(*args, **kw):
            cutovers.append(kw.get("recent_cutover"))
            return real(*args, **kw)
        monkeypatch.setattr(worker_module, "read_step", spy)
        with native_peer(history={101: [native_message(text="<p>older</p>")]}) as peer:
            transport = NativeLoopbackConnector(from_native_server(peer.server, account_id=WACCOUNT, owner_id=OWNER, policy_revision=1))
            default = NativeWorker(store, "tenant-a", (WorkerAccount(WACCOUNT, SyntheticNativeModel(), transport, transport),))
            assert default.history_recovery is False and default.recent_bootstrap is False  # legacy coupling is the default
            paused = NativeWorker(store, "tenant-a", default.accounts, history_recovery=False, recent_bootstrap=True)
            assert paused.history_recovery is False and paused.recent_bootstrap is True
            for _ in range(6):
                paused.step()
        assert cutovers and all(value is True for value in cutovers)
        with Session(engine) as s:
            boot = s.get(RecentBootstrap, (WACCOUNT, 101))
            assert boot is not None and boot.observed_count >= 1
            recovery = s.get(OfRecovery, (WACCOUNT, 101))
            assert recovery.status == "complete" and recovery.high_water > 0
            # The archive import never ran, and was not marked finished to obtain readiness.
            scan = s.get(OfHistoryScan, WACCOUNT)
            assert scan is None or scan.status != "complete"
            assert not s.get(OfRuntime, WACCOUNT).recovery_needed
    finally:
        store.close()
