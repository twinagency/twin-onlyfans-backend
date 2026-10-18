"""Real PostgreSQL acceptance, synthetic data only; missing server is a failure.

ONLYFANS_TEST_PG_URL must identify a disposable test server on which this
suite may create/drop its own uniquely named databases. No provider imports.
"""
import multiprocessing
import os
from pathlib import Path
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db.schema import BASELINE, HEAD, SchemaMismatch, bootstrap, check_schema, upgrade
from db.models import Account, Fan, Message, OfCreatorBinding, OfConversationState, OfMessageDelivery
from db.onlyfans_models import OfIngress, OfRuntime, OfWork, OfRecovery, OfControlAudit
from onlyfans_runtime.store import Store, Event, Refused, utcnow


@pytest.fixture
def database():
    admin_url = os.environ["ONLYFANS_TEST_PG_URL"]
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    name = "of_test_" + uuid.uuid4().hex
    with admin.connect() as c:
        c.exec_driver_sql(f'CREATE DATABASE "{name}" TEMPLATE template0 ENCODING \'UTF8\'')
    url = admin.url.set(database=name).render_as_string(hide_password=False)
    engine = create_engine(url, connect_args={"options": "-c timezone=UTC"})
    try:
        with engine.begin() as c:
            bootstrap(c)
        yield url, engine
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
        admin.dispose()


def seed(url, engine, account="acc-a", tenant="tenant-a", fan=101):
    with Session(engine) as s, s.begin():
        s.add(Account(id=account, nickname="Synthetic account"))
        s.flush()
        s.add(OfCreatorBinding(id="binding-" + account, tenant_id=tenant, creator_id="creator-" + account,
                               creator_name="Synthetic Creator", account_id=account, handle="synthetic", channel="onlyfans"))
        s.add(Fan(account_id=account, fan_id=fan))
        s.flush()
        s.add(OfConversationState(account_id=account, fan_id=fan, mode="automated", revision=0,
                                   takeover_state="none", requires_attention=False))
    store = Store(url)
    store.register(tenant, account, activated_at=utcnow()-timedelta(hours=1), enabled=True)
    return store


def event(mid=1, fan=101, body="Synthetic inbound", **kw):
    return Event(fan, mid, datetime.now(timezone.utc).isoformat(), "in", body, **kw)


def due(engine):
    with engine.begin() as c:
        c.execute(text("UPDATE of_work SET due_at = CURRENT_TIMESTAMP - INTERVAL '1 second'"))


def test_bootstrap_and_refusal_are_explicit(database):
    _, engine = database
    with engine.begin() as c:
        check_schema(c)
        assert c.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
        with pytest.raises(SchemaMismatch, match="empty"):
            bootstrap(c)
        c.execute(text("ALTER TABLE messages RENAME COLUMN body TO broken_body"))
        with pytest.raises(SchemaMismatch, match="messages.body"):
            check_schema(c)
    with engine.connect() as c:
        assert "body" not in {x["name"] for x in inspect(c).get_columns("messages")}, "readiness must not repair"


def test_populated_baseline_upgrade_preserves_evidence(database):
    url, engine = database
    # Rebuild this synthetic DB as the frozen published baseline, then populate it.
    with engine.begin() as c:
        c.exec_driver_sql("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        c.exec_driver_sql((Path(__file__).resolve().parents[1]/"db/snapshots/0070_postgresql.sql").read_text())
        c.exec_driver_sql("CREATE TABLE alembic_version(version_num varchar(32) PRIMARY KEY)")
        c.execute(text("INSERT INTO alembic_version VALUES (:revision)"), {"revision": BASELINE})
    with Session(engine) as s, s.begin():
        s.add(Account(id="old", nickname="Retained")); s.flush()
        s.add(Fan(account_id="old", fan_id=1)); s.flush()
        s.add(Message(account_id="old", fan_id=1, message_id=9, direction="out", body="Retained history", created_at=utcnow())); s.flush()
        # Populate the actual 0070 shape, without current ORM defaults for
        # columns that do not exist until the later migration.
        s.execute(text("INSERT INTO of_conversation_state (account_id,fan_id,mode,revision,unresolved_attempt_id) VALUES ('old',1,'no_contact',4,'unknown-1')"))
        s.execute(text("INSERT INTO of_message_delivery (account_id,fan_id,message_id,outcome,attempt_id,evidence_ref) VALUES ('old',1,9,'unknown','unknown-1','retain')"))
    with engine.begin() as c:
        upgrade(c)
        upgrade(c)
    with Session(engine) as s:
        assert s.get(Message, ("old", 1, 9)).body == "Retained history"
        control = s.get(OfConversationState, ("old", 1))
        assert (control.mode, control.revision, control.unresolved_attempt_id) == ("no_contact", 4, "unknown-1")
        assert s.get(OfMessageDelivery, ("old", 1, 9)).evidence_ref == "retain"
        assert s.get(OfRuntime, "old") is None, "upgrade must not activate an account"


def test_durable_replay_scope_conflict_and_history(database):
    url, engine = database
    store = seed(url, engine)
    incoming = event()
    assert store.record("tenant-a", "acc-a", incoming) == "accepted"
    store.close()
    store = Store(url)
    assert store.record("tenant-a", "acc-a", incoming, source="rest") == "duplicate"
    assert store.drain("tenant-a", "acc-a") == 1
    assert store.record("tenant-a", "acc-a", incoming, source="catchup") == "duplicate"
    assert store.drain("tenant-a", "acc-a") == 0
    with pytest.raises(Refused, match="scope"):
        store.record("tenant-b", "acc-a", event(2))
    other = seed(url, engine, "acc-b", "tenant-b")
    other.record("tenant-b", "acc-b", incoming)
    other.drain("tenant-b", "acc-b")
    store.record("tenant-a", "acc-a", event(1, body="conflicting immutable body"))
    store.drain("tenant-a", "acc-a")
    historical = Event(101, 7, (datetime.now(timezone.utc)-timedelta(days=20)).isoformat(), "in", "History only")
    store.record("tenant-a", "acc-a", historical, source="import")
    store.drain("tenant-a", "acc-a")
    with Session(engine) as s:
        assert s.get(Message, ("acc-a", 101, 1)).body == incoming.body
        assert s.get(Message, ("acc-b", 101, 1)).body == incoming.body
        assert s.get(OfWork, ("acc-a", 101)).generation == 1
        assert s.get(OfRuntime, "acc-a").pending_events == 0
        assert s.scalar(select(OfIngress).where(OfIngress.status == "quarantined")).reason == "conflicting_message_identity"
    store.close(); other.close()


def test_input_revision_tracks_only_committed_new_canonical_history(database, monkeypatch):
    url, engine = database
    store = seed(url, engine)
    def revision(fan=101):
        with Session(engine) as s:
            return s.get(OfConversationState, ("acc-a", fan)).input_revision
    assert revision() == 0
    incoming = event(1)
    assert store.record("tenant-a", "acc-a", incoming) == "accepted"
    store.drain("tenant-a", "acc-a")
    assert revision() == 1
    assert store.record("tenant-a", "acc-a", incoming, source="rest") == "duplicate"
    store.record("tenant-a", "acc-a", event(1), source="catchup")
    store.record("tenant-a", "acc-a", event(1, body="Conflicting identity"))
    store.drain("tenant-a", "acc-a")
    assert revision() == 1, "semantic duplicates and quarantined identity conflicts cannot advance input"
    now = datetime.now(timezone.utc)
    store.record("tenant-a", "acc-a", Event(101, 2, now.isoformat(), "out", "Canonical outbound"))
    store.record("tenant-a", "acc-a", Event(101, 3, (now-timedelta(days=2)).isoformat(), "in", "Imported history"), source="import")
    store.record("tenant-a", "acc-a", Event(101, 4, (now+timedelta(hours=1)).isoformat(), "in", "Future quarantine"))
    store.drain("tenant-a", "acc-a")
    assert revision() == 3, "both directions and imported history change the planner's canonical input"
    store.record("tenant-a", "acc-a", event(1, fan=202))
    store.drain("tenant-a", "acc-a")
    assert revision(202) == 1, "new paused conversations start with the first canonical input"
    apply = store._apply
    def fail_after_apply(s, runtime, row):
        apply(s, runtime, row)
        raise RuntimeError("synthetic failure after revision change")
    monkeypatch.setattr(store, "_apply", fail_after_apply)
    store.record("tenant-a", "acc-a", event(5))
    with pytest.raises(RuntimeError):
        store.drain("tenant-a", "acc-a")
    assert revision() == 3, "an aborted transaction cannot advance canonical input"
    with Session(engine) as s:
        assert s.get(Message, ("acc-a", 101, 5)) is None
    monkeypatch.setattr(store, "_apply", apply)
    store.drain("tenant-a", "acc-a")
    assert revision() == 4
    store.close()


def test_control_invalidates_claim_and_retains_unknown(database):
    url, engine = database
    store = seed(url, engine)
    store.record("tenant-a", "acc-a", event()); store.drain("tenant-a", "acc-a"); due(engine)
    claim = store.claim("tenant-a")
    assert claim
    args = dict(mode="no_contact", expected_revision=0, command_key="stop-1", actor="synthetic-operator", reason="Explicit stop")
    assert store.control("tenant-a", "acc-a", 101, **args) == 1
    assert store.control("tenant-a", "acc-a", 101, **args) == 1
    with pytest.raises(Refused): store.complete("tenant-a", claim)
    with pytest.raises(Refused, match="consent"):
        store.control("tenant-a", "acc-a", 101, mode="automated", expected_revision=1, command_key="resume", actor="operator", reason="resume")
    with Session(engine) as s, s.begin():
        s.get(OfConversationState, ("acc-a",101)).unresolved_attempt_id = "unresolved"
    store.control("tenant-a", "acc-a", 101, mode="automated", expected_revision=1, command_key="consent", actor="operator", reason="new evidence", consent_ref="consent-evidence-1")
    store.record("tenant-a", "acc-a", event(2)); store.drain("tenant-a", "acc-a"); due(engine)
    assert store.claim("tenant-a") is None
    with Session(engine) as s:
        assert s.get(OfConversationState, ("acc-a",101)).unresolved_attempt_id == "unresolved"
    store.close()


@pytest.mark.parametrize("source", ["outbound", "import"])
def test_canonical_history_after_claim_invalidates_completion_and_intent(source, database):
    from onlyfans_runtime.text_delivery import TextAuthority
    url, engine = database
    store = seed(url, engine)
    store.record("tenant-a", "acc-a", event(1))
    store.drain("tenant-a", "acc-a")
    due(engine)
    claim = store.claim("tenant-a")
    assert claim is not None and claim.input_revision == 1
    now = datetime.now(timezone.utc)
    if source == "outbound":
        store.record("tenant-a", "acc-a", Event(101, 2, now.isoformat(), "out", "New canonical outbound"))
    else:
        store.record("tenant-a", "acc-a", Event(101, 2, (now-timedelta(days=2)).isoformat(), "in", "New imported history"), source="import")
    store.drain("tenant-a", "acc-a")
    with Session(engine) as s, s.begin():
        state = s.get(OfConversationState, ("acc-a", 101))
        assert state.input_revision == 2
        assert s.get(OfWork, ("acc-a", 101)).generation == claim.generation
        runtime = s.get(OfRuntime, "acc-a")
        runtime.recovery_needed, runtime.last_sync_at = False, utcnow()
    with pytest.raises(Refused, match="control changed"):
        store.complete("tenant-a", claim)
    authority = TextAuthority(store)
    authority.configure("tenant-a", "acc-a", expected_revision=0, enabled=True, limit_units=10,
                        throttle_seconds=1, actor="synthetic-operator", reason="Synthetic freshness rehearsal")
    with pytest.raises(Refused, match="stale planning claim"):
        authority.prepare("tenant-a", "acc-a", 101, command_key="stale-"+source, expected_revision=0,
                          body="A reply planned before the new canonical history", origin="automated",
                          actor="synthetic-planner", work=claim)
    store.close()


@pytest.mark.parametrize("outcome", ["generating", "queued", "dispatching", "unknown", "partial"])
def test_retained_receipt_without_control_pointer_blocks_claim_and_completion(database, outcome):
    url, engine = database
    store = seed(url, engine)
    other = seed(url, engine, "acc-b", "tenant-a")
    with Session(engine) as s, s.begin():
        s.add(Message(account_id="acc-b", fan_id=101, message_id=1, direction="out", body="Other account", created_at=utcnow()))
        s.flush()
        s.add(OfMessageDelivery(account_id="acc-b", fan_id=101, message_id=1, outcome=outcome, attempt_id="other-account"))
    store.record("tenant-a", "acc-a", event(1000))
    store.drain("tenant-a", "acc-a")
    due(engine)
    claim = store.claim("tenant-a")
    assert claim is not None and claim.account_id == "acc-a", "another account's receipt must not hold this conversation"
    with Session(engine) as s, s.begin():
        # Keep the retained uncertainty outside the latest 100-message read
        # window. Admission must inspect all unresolved evidence, not UI pages.
        s.add_all(Message(account_id="acc-a", fan_id=101, message_id=mid, direction="out", body="Retained history", created_at=utcnow()) for mid in range(1, 102))
        s.flush()
        s.add(OfMessageDelivery(account_id="acc-a", fan_id=101, message_id=1, outcome=outcome, attempt_id="retained-attempt", evidence_ref="retained-evidence"))
        assert s.get(OfConversationState, ("acc-a", 101)).unresolved_attempt_id is None
    with pytest.raises(Refused, match="control changed"):
        store.complete("tenant-a", claim)
    with engine.begin() as c:
        c.execute(text("UPDATE of_work SET lease_until = CURRENT_TIMESTAMP - INTERVAL '1 second'"))
    assert store.claim("tenant-a") is None
    with Session(engine) as s:
        assert s.get(OfWork, ("acc-a", 101)).status == "blocked"
        assert s.get(OfMessageDelivery, ("acc-a", 101, 1)).evidence_ref == "retained-evidence"
    store.close()
    other.close()


def _claim_process(url, ready, go, output):
    store = Store(url)
    ready.put(True); go.wait(10)
    try: output.put(store.claim("tenant-a"))
    finally: store.close()


def test_two_processes_claim_once_and_expired_owner_cannot_complete(database):
    url, engine = database
    store = seed(url, engine)
    store.record("tenant-a", "acc-a", event()); store.drain("tenant-a", "acc-a"); due(engine)
    ctx = multiprocessing.get_context("spawn")
    ready, output, go = ctx.Queue(), ctx.Queue(), ctx.Event()
    workers = [ctx.Process(target=_claim_process, args=(url, ready, go, output)) for _ in range(2)]
    for p in workers: p.start()
    for _ in workers: assert ready.get(timeout=15)
    go.set()
    claims = [output.get(timeout=15) for _ in workers]
    for p in workers: p.join(10); assert p.exitcode == 0
    claimed = [c for c in claims if c is not None]
    assert len(claimed) == 1
    old = claimed[0]
    with engine.begin() as c: c.execute(text("UPDATE of_work SET lease_until = CURRENT_TIMESTAMP - INTERVAL '1 second'"))
    new = store.claim("tenant-a")
    assert new and new.token != old.token
    with pytest.raises(Refused, match="stale"): store.complete("tenant-a", old)
    store.complete("tenant-a", new)
    store.close()


def test_fairness_and_new_input_fencing(database):
    url, engine = database
    store = seed(url, engine)
    other = seed(url, engine, "acc-b", "tenant-a")
    for account in ("acc-a", "acc-b"):
        for mid in range(1, 4): store.record("tenant-a", account, event(mid))
        store.drain("tenant-a", account)
    due(engine)
    first, second = store.claim("tenant-a"), store.claim("tenant-a")
    assert {first.account_id, second.account_id} == {"acc-a", "acc-b"}
    store.record("tenant-a", first.account_id, event(4)); store.drain("tenant-a", first.account_id)
    with pytest.raises(Refused, match="stale"): store.complete("tenant-a", first)
    with Session(engine) as s:
        work=s.get(OfWork,(first.account_id,101))
        assert work.due_at <= work.first_input_at + timedelta(seconds=30)
    store.close(); other.close()


def test_catchup_persists_lower_pages_and_commits_cursor_atomically(database):
    url, engine = database
    store = seed(url, engine)
    store.need_recovery("tenant-a", "acc-a", 101)
    first = store.claim_recovery("tenant-a", "acc-a")
    page = [event(mid) for mid in range(250, 150, -1)]
    assert store.recovery_page("tenant-a", first, page, has_more=True) == "continue"
    with Session(engine) as s:
        cursor=s.get(OfRecovery,("acc-a",101))
        assert (cursor.high_water,cursor.upper_bound,cursor.before_id) == (0,250,151)
    store.close(); store=Store(url)
    second = store.claim_recovery("tenant-a", "acc-a")
    with pytest.raises(Refused, match="cursor"):
        store.recovery_page("tenant-a", second, page, has_more=True)
    assert store.recovery_page("tenant-a", second, [event(mid) for mid in range(150,50,-1)], has_more=True) == "continue"
    third=store.claim_recovery("tenant-a","acc-a")
    assert store.recovery_page("tenant-a",third,[event(mid) for mid in range(50,0,-1)],has_more=False)=="complete"
    while store.drain("tenant-a", "acc-a"): pass
    with Session(engine) as s:
        assert len(s.scalars(select(Message)).all()) == 250
        assert s.get(OfRecovery,("acc-a",101)).high_water == 250
        assert s.get(OfRuntime,"acc-a").recovery_needed, "one conversation is not whole-account completeness"
    with pytest.raises(Refused, match="stale"): store.recovery_page("tenant-a",first,page,has_more=True)
    store.close()


@pytest.mark.parametrize("start", ["reconnect", "backpressure"])
def test_first_account_scan_rechecks_completed_targeted_recovery_and_preserves_lower_gap(database, start):
    from db.onlyfans_models import OfScan
    from onlyfans_runtime.worker import ChatPage, MessagePage, tick
    url, engine = database
    store = seed(url, engine)
    store.need_recovery("tenant-a", "acc-a", 101)
    completed = store.claim_recovery("tenant-a", "acc-a")
    store.recovery_page("tenant-a", completed, [event(1)], has_more=False)
    store.drain("tenant-a", "acc-a")
    store.need_recovery("tenant-a", "acc-a", 202)
    unfinished = store.claim_recovery("tenant-a", "acc-a")
    store.recovery_page("tenant-a", unfinished, [event(250, fan=202), event(200, fan=202)], has_more=True)
    store.close()
    store = Store(url)
    with Session(engine) as s:
        assert s.get(OfScan, "acc-a") is None
        assert s.get(OfRecovery, ("acc-a", 101)).status == "complete"
    if start == "reconnect":
        store.begin_scan("tenant-a", "acc-a")
    else:
        with Session(engine) as s, s.begin():
            runtime = s.get(OfRuntime, "acc-a")
            pending_before = runtime.pending_events
            runtime.pending_events = 1000
        assert store.record("tenant-a", "acc-a", event(999)) == "backpressure"
        with Session(engine) as s, s.begin():
            s.get(OfRuntime, "acc-a").pending_events = pending_before
    with Session(engine) as s:
        old = s.get(OfRecovery, ("acc-a", 101))
        lower = s.get(OfRecovery, ("acc-a", 202))
        assert (old.status, old.high_water, old.before_id) == ("needed", 1, None)
        assert (lower.status, lower.high_water, lower.upper_bound, lower.before_id) == ("needed", 0, 250, 200)

    calls = []
    class SyntheticReader:
        account_id = "acc-a"
        def chats(self, *, cursor, limit):
            return ChatPage([101, 202], None)
        def messages(self, *, fan_id, before_id, limit):
            calls.append((fan_id, before_id))
            return MessagePage([event(2), event(1)], False) if fan_id == 101 else MessagePage([event(199, fan=202)], False)

    for _ in range(10):
        tick(store, "tenant-a", "acc-a", SyntheticReader())
        with Session(engine) as s:
            if not s.get(OfRuntime, "acc-a").recovery_needed:
                break
    else:
        pytest.fail("first account scan did not converge")
    assert (101, None) in calls and (202, 200) in calls
    with Session(engine) as s:
        assert s.get(Message, ("acc-a", 101, 2)) is not None
        assert s.get(OfRecovery, ("acc-a", 101)).high_water == 2
        assert s.get(OfRecovery, ("acc-a", 202)).high_water == 250
    store.close()


def test_backpressure_and_missing_control_fail_closed(database):
    url, engine=database
    store=seed(url,engine)
    with Session(engine) as s,s.begin():s.get(OfRuntime,"acc-a").pending_events=1000
    assert store.record("tenant-a","acc-a",event())=="backpressure"
    with Session(engine) as s,s.begin():s.get(OfRuntime,"acc-a").pending_events=0
    store.record("tenant-a","acc-a",event(fan=202));store.drain("tenant-a","acc-a");due(engine)
    assert store.claim("tenant-a") is None
    with Session(engine) as s:
        assert s.get(OfConversationState,("acc-a",202)).mode=="paused"
        assert s.get(OfWork,("acc-a",202)).status=="blocked"
    with pytest.raises(Refused):store.record("tenant-a","acc-a",event(body="x"*20001))
    store.close()


def test_repeated_conversation_gap_preserves_lower_cursor_and_repeats_upper_window(database):
    from db.onlyfans_models import OfScan
    url, engine = database
    store = seed(url, engine)
    store.begin_scan("tenant-a", "acc-a")
    scan = store.claim_scan("tenant-a", "acc-a")
    store.scan_page("tenant-a", scan, [101], next_cursor=None)
    first = store.claim_recovery("tenant-a", "acc-a")
    store.recovery_page("tenant-a", first, [event(3), event(2)], has_more=True)
    store.need_recovery("tenant-a", "acc-a", 101)
    lower = store.claim_recovery("tenant-a", "acc-a")
    assert (lower.upper_bound, lower.before_id) == (3, 2)
    store.recovery_page("tenant-a", lower, [event(1)], has_more=False)
    store.drain("tenant-a", "acc-a")
    with Session(engine) as s:
        assert s.get(OfRuntime, "acc-a").recovery_needed
        assert s.get(OfScan, "acc-a").status == "needed"
    scan = store.claim_scan("tenant-a", "acc-a")
    store.scan_page("tenant-a", scan, [101], next_cursor=None)
    newer = store.claim_recovery("tenant-a", "acc-a")
    assert newer.high_water == 3 and newer.before_id is None
    store.recovery_page("tenant-a", newer, [event(4)], has_more=False)
    store.drain("tenant-a", "acc-a")
    with Session(engine) as s:
        assert not s.get(OfRuntime, "acc-a").recovery_needed
        assert len(s.scalars(select(Message)).all()) == 4
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    store.record("tenant-a", "acc-a", Event(101, 5, future, "in", "Invalid future history"))
    store.drain("tenant-a", "acc-a")
    with Session(engine) as s:
        assert s.get(OfRuntime, "acc-a").recovery_needed, "quarantined history cannot leave sync current"
        assert s.get(Message, ("acc-a", 101, 5)) is None
    store.close()


def test_discovery_restart_recovers_new_fans_and_only_then_marks_current(database):
    from onlyfans_runtime.worker import ChatPage, MessagePage, tick
    url, engine = database
    store = seed(url, engine)
    class SyntheticReader:
        account_id = "acc-a"
        def chats(self, *, cursor, limit):
            assert limit == 100
            return ChatPage([202], "page-2") if cursor is None else ChatPage([303], None)
        def messages(self, *, fan_id, before_id, limit):
            return MessagePage([event(2, fan=fan_id), event(1, fan=fan_id)], False)
    reader = SyntheticReader()
    store.begin_scan("tenant-a", "acc-a")
    tick(store, "tenant-a", "acc-a", reader)
    store.close(); store = Store(url)
    # Restart during discovery: preserve the cursor, then repeat a full pass
    # to cover arrivals above a saved message window's upper bound.
    store.begin_scan("tenant-a", "acc-a")
    for _ in range(20):
        tick(store, "tenant-a", "acc-a", reader)
        with Session(engine) as s:
            if not s.get(OfRuntime, "acc-a").recovery_needed: break
    else: pytest.fail("durable recovery did not converge")
    with Session(engine) as s:
        last_sync = s.get(OfRuntime,"acc-a").last_sync_at
        assert last_sync is not None
        assert s.get(Message,("acc-a",303,1)) is not None
        assert len(s.scalars(select(Message)).all()) == 6
        assert s.get(OfConversationState,("acc-a",202)).mode == "paused"
    store.drain("tenant-a", "acc-a")
    with Session(engine) as s, s.begin():
        runtime = s.get(OfRuntime,"acc-a")
        assert runtime.last_sync_at == last_sync, "idle drains must not make old data look freshly synchronized"
        runtime.pending_events = 1000
    assert store.record("tenant-a", "acc-a", event(3, fan=404)) == "backpressure"
    with Session(engine) as s, s.begin(): s.get(OfRuntime,"acc-a").pending_events = 0
    store.drain("tenant-a", "acc-a")
    with Session(engine) as s:
        assert s.get(OfRuntime,"acc-a").recovery_needed, "draining capacity cannot erase a newly recorded discovery gap"
    store.close()


def test_failed_apply_rolls_back_effects_and_replay_recovers(database, monkeypatch):
    url, engine = database
    store=seed(url,engine)
    store.record("tenant-a","acc-a",event())
    apply=store._apply
    def crash_after_apply(s,runtime,row):
        apply(s,runtime,row)
        raise RuntimeError("synthetic crash before transaction commit")
    monkeypatch.setattr(store,"_apply",crash_after_apply)
    with pytest.raises(RuntimeError):store.drain("tenant-a","acc-a")
    with Session(engine) as s:
        assert s.get(Message,("acc-a",101,1)) is None
        assert s.get(OfWork,("acc-a",101)) is None
        assert s.get(OfRuntime,"acc-a").pending_events==1
        assert s.scalar(select(OfIngress)).status=="pending"
    monkeypatch.setattr(store,"_apply",apply)
    assert store.drain("tenant-a","acc-a")==1
    due(engine)
    claim=store.claim("tenant-a")
    with Session(engine) as s,s.begin():
        s.get(OfCreatorBinding,"binding-acc-a").revoked_at=utcnow()
    with pytest.raises(Refused,match="scope"):store.complete("tenant-a",claim)
    store.close()


def test_readiness_refuses_wrong_key_index_and_unknown_revision(database):
    _,engine=database
    for ddl in (
        "ALTER TABLE grok_daily_cost DROP CONSTRAINT pk_grok_daily_cost; ALTER TABLE grok_daily_cost ADD PRIMARY KEY(day)",
        "DROP INDEX uq_of_creator_bindings_active_account",
        "UPDATE alembic_version SET version_num='unrecognized_legacy'",
        "ALTER TABLE of_runtime DROP CONSTRAINT ck_of_runtime_ck_of_runtime_pending",
    ):
        with engine.connect() as c:
            tx=c.begin()
            c.exec_driver_sql(ddl)
            with pytest.raises(SchemaMismatch):check_schema(c)
            with pytest.raises(SchemaMismatch):upgrade(c)
            tx.rollback()
    with engine.connect() as c:check_schema(c)


def test_supported_populated_0064_upgrade_preserves_identity(database):
    _,engine=database
    with engine.begin() as c:
        c.exec_driver_sql("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        c.exec_driver_sql((Path(__file__).resolve().parents[1]/"db/snapshots/0070_postgresql.sql").read_text())
        c.exec_driver_sql("DROP TABLE of_message_delivery; DROP TABLE of_conversation_state; DROP TABLE of_creator_bindings")
        c.exec_driver_sql("CREATE TABLE alembic_version(version_num varchar(32) PRIMARY KEY); INSERT INTO alembic_version VALUES ('0064_user_llm_keys')")
    with Session(engine) as s,s.begin():s.add(Account(id="retained-0064",nickname="Existing account"))
    with engine.begin() as c:upgrade(c)
    with Session(engine) as s:
        assert s.get(Account,"retained-0064").nickname=="Existing account"
        assert s.get(OfRuntime,"retained-0064") is None
