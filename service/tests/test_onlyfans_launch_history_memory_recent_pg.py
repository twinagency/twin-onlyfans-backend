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
    RecoveryBase.metadata.create_all(engine)
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
