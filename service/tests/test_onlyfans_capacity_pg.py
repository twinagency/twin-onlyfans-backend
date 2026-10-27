"""Small measured synthetic rehearsal; not a production capacity certification."""
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime, timezone
import json
import multiprocessing
import os
from pathlib import Path
import time

from sqlalchemy import create_engine, event as sqlalchemy_event, select, func, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
import pytest

from test_onlyfans_foundation_pg import database, seed, due, event
from db.models import Fan, Message, OfConversationState
from db.onlyfans_models import OfIngress, OfRuntime, OfWork
from onlyfans_runtime.store import Store, MAX_PENDING
from onlyfans_runtime.worker import ChatPage, MessagePage, tick


def complete_worker(url, tenant):
    store = Store(url)
    completed = []
    try:
        while claim := store.claim(tenant):
            store.complete(tenant, claim)
            completed.append((claim.account_id, claim.fan_id, claim.generation))
    finally:
        store.close()
    return completed


def native_delivery_worker(url, tenant):
    """One invocation per claimed input; queued pacing retries reuse the intent."""
    from onlyfans_runtime.native_planning import NativeTextPlanner,SyntheticNativeModel
    from onlyfans_runtime.text_delivery import SyntheticTextTransport
    store=Store(url);planner=NativeTextPlanner(store)
    completed=[];model_calls=wire_calls=0
    try:
        while claim:=store.claim(tenant):
            model=SyntheticNativeModel("That sounds peaceful. How is the weather where you are?",reported_cost_millicents=7)
            plan=planner.prepare(tenant,claim,model)
            intent=planner.queue(tenant,claim.account_id,plan)
            wire=SyntheticTextTransport(claim.account_id,message_id=10000+claim.fan_id)
            deadline=time.monotonic()+20
            while True:
                status=planner.authority.dispatch(tenant,claim.account_id,intent,wire)
                if status!="queued":break
                if time.monotonic()>deadline:raise AssertionError("synthetic paced intent did not settle")
                time.sleep(.025)
            assert status=="accepted" and len(model.calls)==len(wire.calls)==1
            completed.append((claim.account_id,claim.fan_id,plan,intent))
            model_calls+=len(model.calls);wire_calls+=len(wire.calls)
    finally:store.close()
    return completed,model_calls,wire_calls


def report(name, value):
    print(json.dumps({"rehearsal": name, **value}, sort_keys=True))
    if folder := os.environ.get("ONLYFANS_TEST_CAPACITY_EVIDENCE"):
        path = Path(folder)
        path.mkdir(parents=True, exist_ok=True)
        (path / (name + ".json")).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


@pytest.mark.parametrize("account_scope", [None, ("race-first",)])
def test_claim_rechecks_exhausted_account_without_losing_fairness_or_scope(database, account_scope):
    """Pause the eligibility snapshot before its runtime lock, then drain it."""
    url, engine = database
    waiting = seed(url, engine, "race-first")
    competitor = seed(url, engine, "race-next")
    for account in ("race-first", "race-next"):
        assert waiting.record("tenant-a", account, event()) == "accepted"
        assert waiting.drain("tenant-a", account) == 1
    due(engine)
    barrier_key = 8312347
    with engine.begin() as c:
        c.exec_driver_sql("""CREATE FUNCTION claim_snapshot_barrier() RETURNS boolean
            LANGUAGE plpgsql VOLATILE AS $$ BEGIN
            PERFORM pg_advisory_xact_lock(8312347); RETURN true; END $$""")

    pause_once = True
    def pause_selection(connection, cursor, statement, parameters, context, executemany):
        nonlocal pause_once
        if pause_once and "FOR UPDATE OF of_runtime SKIP LOCKED" in statement:
            pause_once = False
            statement = statement.replace(" ORDER BY ", " AND claim_snapshot_barrier() ORDER BY ", 1)
        return statement, parameters

    sqlalchemy_event.listen(waiting.engine, "before_cursor_execute", pause_selection, retval=True)
    try:
        with engine.connect() as gate, ThreadPoolExecutor(max_workers=1) as pool:
            gate.execute(text("SELECT pg_advisory_lock(:key)"), {"key": barrier_key})
            future = pool.submit(waiting.claim, "tenant-a", account_ids=account_scope)
            try:
                deadline = time.monotonic() + 10
                while not gate.scalar(text("""SELECT EXISTS (SELECT FROM pg_locks
                        WHERE locktype='advisory' AND objid=:key AND NOT granted)"""), {"key": barrier_key}):
                    assert time.monotonic() < deadline, "claim did not reach its eligibility snapshot barrier"
                    time.sleep(.01)
                # This real competing worker changes the work after the waiting
                # SELECT took its snapshot, before it acquires the runtime lock.
                first = competitor.claim("tenant-a", account_ids=("race-first",))
                assert first and first.account_id == "race-first"
                competitor.complete("tenant-a", first)
            finally:
                gate.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": barrier_key})
            next_claim = future.result(timeout=10)
        if account_scope is None:
            assert next_claim and next_claim.account_id == "race-next"
        else:
            assert next_claim is None, "an exhausted scoped account must not claim another account"
            next_claim = competitor.claim("tenant-a")
            assert next_claim and next_claim.account_id == "race-next"
        competitor.complete("tenant-a", next_claim)
        assert waiting.claim("tenant-a") is None
        with Session(engine) as s:
            work = s.scalars(select(OfWork).order_by(OfWork.account_id)).all()
            # One generation for accepted input and one for its sole claim.
            assert len(work) == 2 and all(row.status == "done" and row.generation == 2 for row in work)
    finally:
        sqlalchemy_event.remove(waiting.engine, "before_cursor_execute", pause_selection)
        waiting.close()
        competitor.close()


def test_measured_account_fairness_competing_processes_and_real_db_outage(database):
    url, engine = database
    started = time.monotonic()
    store = seed(url, engine, "load-00")
    for index in range(1, 12):
        other = seed(url, engine, f"load-{index:02}")
        other.close()
    # New discovered conversations correctly default to paused. This explicit
    # synthetic cohort grants automated control before testing ready-work load.
    with Session(engine) as s, s.begin():
        for index in range(12):
            for fan in range(102, 117):
                s.add(Fan(account_id=f"load-{index:02}", fan_id=fan))
        s.flush()
        for index in range(12):
            for fan in range(102, 117):
                s.add(OfConversationState(account_id=f"load-{index:02}", fan_id=fan, mode="automated", revision=0))
    for index in range(12):
        for fan in range(101, 117):
            incoming = event(mid=1, fan=fan)
            assert store.record("tenant-a", f"load-{index:02}", incoming) == "accepted"
            assert store.record("tenant-a", f"load-{index:02}", incoming, source="rest") == "duplicate"
        assert store.drain("tenant-a", f"load-{index:02}") == 16
    ingestion_seconds = time.monotonic() - started
    due(engine)  # Test fixture excludes the deliberate seven-second quiet delay.
    first = [store.claim("tenant-a") for _ in range(12)]
    assert all(first) and len({claim.account_id for claim in first}) == 12
    for claim in first:
        store.complete("tenant-a", claim)
    dispatch_start = time.monotonic()
    with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as pool:
        groups = list(pool.map(complete_worker, [url] * 4, ["tenant-a"] * 4))
    remaining = [item for group in groups for item in group]
    all_claims = [(c.account_id, c.fan_id, c.generation) for c in first] + remaining
    assert len(all_claims) == len(set(all_claims)) == 192
    claim_seconds = time.monotonic() - dispatch_start
    # A real connection outage, confined to this disposable DB. Clients lose
    # their pools; retry after restoration uses canonical event deduplication.
    admin = create_engine(os.environ["ONLYFANS_TEST_PG_URL"], isolation_level="AUTOCOMMIT")
    name = engine.url.database
    outage_start = time.monotonic()
    try:
        with admin.connect() as c:
            c.exec_driver_sql(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS false')
            c.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=:name"), {"name": name})
        with pytest.raises(SQLAlchemyError):
            store.record("tenant-a", "load-00", event(mid=2))
    finally:
        with admin.connect() as c:
            c.exec_driver_sql(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS true')
        admin.dispose()
        # This assertion engine intentionally has no production pool_pre_ping;
        # its cached test-only sockets were terminated by the outage as well.
        engine.dispose()
    recovered = event(mid=2)
    assert store.record("tenant-a", "load-00", recovered) == "accepted"
    assert store.record("tenant-a", "load-00", recovered, source="catchup") == "duplicate"
    assert store.drain("tenant-a", "load-00") == 1
    recovery_seconds = time.monotonic() - outage_start
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(Message)) == 193
        assert s.scalar(select(func.count()).select_from(OfWork).where(OfWork.status == "done")) == 191
        assert s.scalar(select(func.count()).select_from(OfIngress).where(OfIngress.status == "pending")) == 0
        assert sum(s.scalars(select(OfRuntime.pending_events))) == 0
    store.close()
    report("fairness-outage", {
        "scope": "synthetic database/intake/claim rehearsal; no model or provider", "accounts": 12,
        "conversations": 192, "competingProcesses": 4, "uniqueCompletedClaims": 192,
        "duplicateClaims": 0, "firstTwelveClaimsCoverTwelveAccounts": True,
        "ingestionIncludingSetupSeconds": round(ingestion_seconds, 3),
        "remainingClaimsSeconds": round(claim_seconds, 3),
        "remainingClaimsPerSecond": round(len(remaining) / claim_seconds, 3),
        "databaseOutageAndRecoverySeconds": round(recovery_seconds, 3),
        "canonicalMessagesAfterRecovery": 193, "providerRequests": 0,
        "limits": "shared ephemeral sandbox; quiet window advanced by test fixture; no end-to-end reply latency or production capacity claim",
    })


def test_measured_native_preparation_cost_and_one_use_delivery_across_workers(database,monkeypatch):
    from db.models import AccountAiConfig,GrokCall,GrokDailyCost,OfMessageDelivery
    from db.onlyfans_delivery_models import OfNativePlan,OfDeliveryPolicy,OfTextIntent
    from onlyfans_runtime.text_delivery import TextAuthority
    url,engine=database
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","worker");monkeypatch.setenv("DATABASE_URL",url)
    accounts=[f"native-load-{index:02}" for index in range(12)]
    fans=range(101,117)
    for account in accounts:
        store=seed(url,engine,account)
        with Session(engine) as s,s.begin():
            runtime=s.get(OfRuntime,account);runtime.recovery_needed=False;runtime.last_sync_at=datetime.now(timezone.utc).replace(tzinfo=None)
            s.add(AccountAiConfig(account_id=account,model="grok-4-1-fast-non-reasoning",persona="Synthetic Creator enjoys hiking and coffee.",daily_cost_cap_cents=100))
            for fan in range(102,117):s.add(Fan(account_id=account,fan_id=fan))
            s.flush()
            for fan in range(102,117):s.add(OfConversationState(account_id=account,fan_id=fan,mode="automated",revision=0))
        TextAuthority(store).configure("tenant-a",account,expected_revision=0,enabled=True,limit_units=20,
            throttle_seconds=1,actor="synthetic-capacity",reason="Offline native worker rehearsal")
        for fan in fans:store.record("tenant-a",account,event(fan=fan,body="How has your day been?"))
        store.drain("tenant-a",account);store.close()
    due(engine) # Exclude only the initial input-formation quiet window.
    started=time.monotonic()
    with ProcessPoolExecutor(max_workers=4,mp_context=multiprocessing.get_context("spawn")) as pool:
        groups=list(pool.map(native_delivery_worker,[url]*4,["tenant-a"]*4))
    elapsed=time.monotonic()-started
    completed=[row for group,_,_ in groups for row in group]
    assert len(completed)==len({row[:2] for row in completed})==192
    assert sum(models for _,models,_ in groups)==sum(wire for _,_,wire in groups)==192
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(OfNativePlan))==192
        assert s.scalar(select(func.count()).select_from(GrokCall).where(GrokCall.status=="done"))==192
        assert s.scalar(select(func.count()).select_from(Message).where(Message.direction=="out"))==192
        assert s.scalar(select(func.count()).select_from(OfMessageDelivery).where(OfMessageDelivery.outcome=="sent"))==192
        assert s.scalar(select(func.count()).select_from(OfTextIntent).where(OfTextIntent.status=="accepted",OfTextIntent.wire_started_at.is_not(None)))==192
        assert s.scalar(select(func.sum(GrokDailyCost.cost_cents)))==1344
        policies=s.scalars(select(OfDeliveryPolicy)).all()
        assert len(policies)==12 and all(p.used_units==16 and p.limit_units==20 and p.active_intent_id is None and p.queued_count==0 for p in policies)
    report("native-preparation-delivery",{"scope":"synthetic native planning, known synthetic cost and one-use acceptance; no external model/provider",
        "accounts":12,"conversations":192,"competingProcesses":4,"nativeInvocations":192,"syntheticWireCalls":192,
        "acceptedCanonicalMessages":192,"costMillicents":1344,"deliveryUnitsPerAccount":16,"limitUnitsPerAccount":20,
        "elapsedSeconds":round(elapsed,3),"completedPerSecond":round(192/elapsed,3),"providerRequests":0,
        "limits":"shared ephemeral sandbox; synthetic zero-latency model/transport; real one-second account pacing retained; excludes initial quiet window; no paid-provider latency or production/fleet capacity claim"})


class OverflowReader:
    account_id = "hot"

    def __init__(self, events): self.events = events
    def chats(self, *, cursor, limit):
        assert cursor is None
        return ChatPage([101], None)
    def messages(self, *, fan_id, before_id, limit):
        assert fan_id == 101
        candidates = [e for e in self.events if before_id is None or e.message_id < before_id]
        selected = sorted(candidates, key=lambda e: e.message_id, reverse=True)[:limit]
        return MessagePage(selected, len(candidates) > len(selected))


def test_hot_account_bounded_backpressure_recovers_missing_events_without_starving_cold(database):
    url, engine = database
    store = seed(url, engine, "hot")
    other = seed(url, engine, "cold")
    other.close()
    started = time.monotonic()
    incoming = [event(mid=i) for i in range(1, MAX_PENDING + 26)]
    for message in incoming[:MAX_PENDING]:
        assert store.record("tenant-a", "hot", message) == "accepted"
    for message in incoming[MAX_PENDING:]:
        assert store.record("tenant-a", "hot", message) == "backpressure"
    with Session(engine) as s:
        assert s.get(OfRuntime, "hot").pending_events == MAX_PENDING
        assert s.get(OfRuntime, "hot").recovery_needed
    store.record("tenant-a", "cold", event())
    store.drain("tenant-a", "cold")
    due(engine)
    claim = store.claim("tenant-a")
    assert claim and claim.account_id == "cold", "a saturated account must not starve an independent cold account"
    store.complete("tenant-a", claim)
    pages = 0
    reader = OverflowReader(incoming)
    while pages < 80:
        tick(store, "tenant-a", "hot", reader)
        pages += 1
        with Session(engine) as s:
            runtime = s.get(OfRuntime, "hot")
            if not runtime.recovery_needed and runtime.pending_events == 0:
                break
    else: raise AssertionError("bounded catch-up did not complete")
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(Message).where(Message.account_id == "hot")) == MAX_PENDING + 25
        assert s.scalar(select(func.count()).select_from(OfIngress).where(OfIngress.status == "quarantined")) == 0
    store.close()
    report("overload-catchup", {"scope": "synthetic intake and recovery; no provider",
        "pendingBound": MAX_PENDING, "backpressuredEvents": 25, "recoveredCanonicalMessages": MAX_PENDING + 25,
        "coldAccountProgressedBeforeHotRecovery": True, "recoveryTicks": pages,
        "elapsedSeconds": round(time.monotonic()-started, 3), "providerRequests": 0,
        "limits": "single hot account; bounded rehearsal, not a soak or full product capacity test"})
