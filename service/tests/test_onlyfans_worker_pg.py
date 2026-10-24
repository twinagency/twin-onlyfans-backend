"""Full worker phases, process crashes and local HTTP; synthetic state only."""
from datetime import timedelta
import json
import multiprocessing
import os
import threading

import pytest
from sqlalchemy import create_engine,func,select,text
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database,seed,event,due
from test_onlyfans_text_delivery_pg import configured,TENANT,ACCOUNT,accepted
from test_onlyfans_loopback_transport_pg import peer_server,transport_for
from db.models import AccountAiConfig,Fan,Message,GrokCall,GrokDailyCost,OfCreatorBinding,OfConversationState
from db.onlyfans_models import OfRuntime,OfWork
from db.onlyfans_delivery_models import OfNativePlan,OfTextIntent,OfDeliveryPolicy
from onlyfans_runtime.store import Store,Refused,utcnow
from onlyfans_runtime.native_planning import NativeTextPlanner,SyntheticNativeModel
from onlyfans_runtime.text_delivery import TextAuthority,SyntheticTextTransport
from onlyfans_runtime.native_worker import NativeWorker,WorkerAccount,CycleReport


def start(database,monkeypatch):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","worker");monkeypatch.setenv("DATABASE_URL",database[0])
    store,authority=configured(database,mode="automated")
    with Session(database[1]) as s,s.begin():
        s.add(AccountAiConfig(account_id=ACCOUNT,model="grok-4-1-fast-non-reasoning",
            persona="Synthetic Creator enjoys hiking and coffee.",daily_cost_cap_cents=100))
    store.record(TENANT,ACCOUNT,event(body="How has your day been?"));store.drain(TENANT,ACCOUNT);due(database[1])
    return store,authority


def worker(store,*,model=None,transport=None,account=ACCOUNT,tenant=TENANT):
    return NativeWorker(store,tenant,(WorkerAccount(account,model or SyntheticNativeModel(reported_cost_millicents=7),
        transport or SyntheticTextTransport(account)),))


def test_actual_worker_runs_native_cost_and_exact_http_delivery_once(database,monkeypatch):
    store,_=start(database,monkeypatch);model=SyntheticNativeModel(reported_cost_millicents=7)
    def inspect(record):
        with Session(database[1]) as s:
            intent=s.scalar(select(OfTextIntent));assert intent.wire_started_at is not None
            assert s.scalar(select(func.count()).select_from(Message).where(Message.direction=="out"))==0
            assert record["json"]["payload"]["text"]==model.body
    with peer_server(on_request=inspect) as peer:
        runtime=worker(store,model=model,transport=transport_for(peer))
        result=runtime.step();assert (result.claims,result.queued,result.accepted,result.errors)==(1,1,1,0)
        for _ in range(3):assert runtime.step().claims==0
        assert len(peer.records)==len(model.calls)==1
    with Session(database[1]) as s:
        assert s.scalar(select(GrokDailyCost)).cost_cents==7
        assert s.scalar(select(Message).where(Message.direction=="out")).body==model.body
        assert s.get(OfDeliveryPolicy,ACCOUNT).used_units==1
    assert set(result.public())=={"schemaVersion",*CycleReport.__dataclass_fields__}
    assert model.body not in json.dumps(result.public()) and ACCOUNT not in json.dumps(result.public())
    store.close()


@pytest.mark.parametrize("mode",["accepted_drop","not_accepted","wrong_hash"])
def test_worker_does_not_retry_uncertain_or_rejected_http_and_retains_cost(database,monkeypatch,mode):
    store,_=start(database,monkeypatch);model=SyntheticNativeModel(reported_cost_millicents=7)
    with peer_server(mode) as peer:
        runtime=worker(store,model=model,transport=transport_for(peer))
        result=runtime.step()
        assert result.not_accepted==int(mode=="not_accepted")
        assert result.unknown==int(mode!="not_accepted")
        for _ in range(3):runtime.step()
        assert len(peer.records)==len(model.calls)==1
    with Session(database[1]) as s:
        assert s.scalar(select(func.count()).select_from(Message).where(Message.direction=="out"))==0
        assert s.get(OfDeliveryPolicy,ACCOUNT).used_units==int(mode!="not_accepted")
        assert s.scalar(select(GrokDailyCost)).cost_cents==7
    store.close()


def die(url,phase):
    os.environ.update(ONLYFANS_PROCESS_ROLE="worker",DATABASE_URL=url)
    store=Store(url);planner=NativeTextPlanner(store);authority=TextAuthority(store)
    claim=store.claim(TENANT)
    model=SyntheticNativeModel(reported_cost_millicents=7,on_generate=(lambda:os._exit(77)) if phase=="generating" else None)
    plan=planner.prepare(TENANT,claim,model)
    if phase=="ready":os._exit(77)
    identity=planner.queue(TENANT,ACCOUNT,plan)
    if phase=="queued":os._exit(77)
    grant=authority.admit(TENANT,ACCOUNT,identity)
    if phase=="admitted":os._exit(77)
    wire=SyntheticTextTransport(ACCOUNT)
    proof=wire.send(grant,authority=authority,tenant_id=TENANT)
    authority.record_accepted(TENANT,ACCOUNT,identity,grant.token,proof)
    os._exit(77)


@pytest.mark.parametrize("phase",["generating","ready","queued","admitted","accepted"])
def test_process_death_recovers_each_durable_phase_without_second_model_or_send(database,monkeypatch,phase):
    store,_=start(database,monkeypatch)
    child=multiprocessing.get_context("spawn").Process(target=die,args=(database[0],phase));child.start();child.join(20)
    assert child.exitcode==77
    if phase in {"generating","admitted"}:
        with Session(database[1]) as s,s.begin():
            if phase=="generating":s.get(OfWork,(ACCOUNT,101)).lease_until=utcnow()-timedelta(seconds=1)
            else:s.scalar(select(OfTextIntent)).deadline=utcnow()-timedelta(seconds=1)
    model=SyntheticNativeModel();wire=SyntheticTextTransport(ACCOUNT);runtime=worker(store,model=model,transport=wire)
    report=runtime.step()
    for _ in range(2):runtime.step()
    assert model.calls==[] and len(wire.calls)==int(phase in {"ready","queued"})
    with Session(database[1]) as s:
        assert s.scalar(select(func.count()).select_from(GrokCall))==1
        outbound=s.scalar(select(func.count()).select_from(Message).where(Message.direction=="out"))
        assert outbound==int(phase in {"ready","queued","accepted"})
        plan=s.scalar(select(OfNativePlan))
        if phase=="generating":
            assert plan.status=="unknown" and s.scalar(select(GrokCall)).status=="pending"
            assert s.scalar(select(GrokDailyCost)).cost_cents>0
        else:assert s.scalar(select(GrokDailyCost)).cost_cents==7
        if phase=="admitted":
            assert s.scalar(select(OfTextIntent)).status=="unknown"
            assert s.get(OfDeliveryPolicy,ACCOUNT).used_units==1
        if phase=="accepted":assert report.repaired==1
    store.close()


@pytest.mark.parametrize("change",["expired_lease","config","input","pause","revoke"])
def test_ready_recovery_never_renews_or_rewrites_old_authority(database,monkeypatch,change):
    store,_=start(database,monkeypatch);planner=NativeTextPlanner(store);claim=store.claim(TENANT)
    model=SyntheticNativeModel(reported_cost_millicents=7);identity=planner.prepare(TENANT,claim,model)
    with Session(database[1]) as s:original=s.get(OfNativePlan,(ACCOUNT,identity)).claim_json
    if change=="input":
        store.record(TENANT,ACCOUNT,event(mid=2,body="A fresh correction."));store.drain(TENANT,ACCOUNT)
    elif change=="pause":store.control(TENANT,ACCOUNT,101,mode="paused",expected_revision=0,command_key="synthetic-pause",actor="synthetic",reason="Stop")
    else:
        with Session(database[1]) as s,s.begin():
            if change=="expired_lease":s.get(OfWork,(ACCOUNT,101)).lease_until=utcnow()-timedelta(seconds=1)
            elif change=="config":s.get(AccountAiConfig,ACCOUNT).persona="Corrected approved creator persona."
            else:s.get(OfCreatorBinding,"binding-acc-a").revoked_at=utcnow()
    replacement=SyntheticNativeModel();wire=SyntheticTextTransport(ACCOUNT)
    worker(store,model=replacement,transport=wire).step()
    assert replacement.calls==[] and wire.calls==[]
    with Session(database[1]) as s:
        plan=s.get(OfNativePlan,(ACCOUNT,identity));assert plan.claim_json==original
        assert plan.status==("ready" if change=="revoke" else "stale")
        assert s.scalar(select(func.count()).select_from(GrokCall))==1
        assert s.scalar(select(GrokDailyCost)).cost_cents==7
    store.close()


def test_stop_after_model_keeps_ready_evidence_and_replacement_finishes_it(database,monkeypatch):
    store,_=start(database,monkeypatch);stop=threading.Event()
    model=SyntheticNativeModel(on_generate=stop.set,reported_cost_millicents=7);wire=SyntheticTextTransport(ACCOUNT)
    first=worker(store,model=model,transport=wire)
    assert first.run(stop,max_cycles=10,poll_seconds=.05)==1 and wire.calls==[]
    with Session(database[1]) as s:assert s.scalar(select(OfNativePlan)).status=="ready"
    replacement=SyntheticNativeModel();second=worker(store,model=replacement,transport=wire)
    assert second.step().accepted==1 and replacement.calls==[] and len(wire.calls)==1
    store.close()


def test_stop_during_committed_wire_finishes_receipt_and_starts_no_other_work(database,monkeypatch):
    store,_=start(database,monkeypatch);stop=threading.Event()
    wire=SyntheticTextTransport(ACCOUNT,on_send=lambda _:stop.set())
    runtime=worker(store,transport=wire);observed=[]
    assert runtime.run(stop,max_cycles=5,poll_seconds=.05,observe=observed.append)==1
    assert observed[0]["accepted"]==1 and observed[0]["errors"]==0
    with Session(database[1]) as s:assert s.scalar(select(OfTextIntent)).projected_at is not None
    store.close()


def test_account_allowlist_never_claims_unregistered_scope_and_rotation_is_bounded(database,monkeypatch):
    store,_=start(database,monkeypatch)
    other=seed(*database,account="acc-other");other.record(TENANT,"acc-other",event());other.drain(TENANT,"acc-other");due(database[1])
    runtime=worker(store);assert runtime.step().accepted==1
    with Session(database[1]) as s:assert s.get(OfWork,("acc-other",101)).status=="waiting"
    assert store.claim(TENANT,account_ids=("unknown",)) is None
    for invalid in ((),("acc-a","acc-a"),(["x"],),["acc-a"],("x"*65,)):
        with pytest.raises(Refused):store.claim(TENANT,account_ids=invalid)
    assert worker(store,tenant="different-tenant").step().held==1
    assert store.claim(TENANT,account_ids=("acc-other",)).account_id=="acc-other"
    store.close();other.close()


def test_model_uncertainty_and_delivery_cap_do_not_create_retry_storms(database,monkeypatch):
    store,_=start(database,monkeypatch);model=SyntheticNativeModel(fail=True);runtime=worker(store,model=model)
    assert runtime.step().held==1
    for _ in range(4):runtime.step()
    assert len(model.calls)==1
    with Session(database[1]) as s,s.begin():
        s.get(OfWork,(ACCOUNT,101)).status="waiting"
        policy=s.get(OfDeliveryPolicy,ACCOUNT);policy.used_units=policy.limit_units
    assert runtime.step().claims==0 and len(model.calls)==1
    store.close()


def test_cached_worker_refuses_role_switch_and_health_contains_only_counts(database,monkeypatch):
    store,_=start(database,monkeypatch);runtime=worker(store)
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","api")
    with pytest.raises(Refused):runtime.step()
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","worker")
    with pytest.raises(Refused):NativeWorker(store,"deployment",runtime.accounts)
    with pytest.raises(Refused):NativeWorker(store,TENANT,(WorkerAccount(ACCOUNT,object(),runtime.accounts[0].transport),))
    stop=threading.Event();stop.set();assert runtime.run(stop)==0
    store.close()


def compete_worker(url,ready,gate,done,output):
    os.environ.update(ONLYFANS_PROCESS_ROLE="worker",DATABASE_URL=url)
    store=Store(url);model=SyntheticNativeModel(delay_seconds=.2,reported_cost_millicents=7);wire=SyntheticTextTransport(ACCOUNT)
    from sqlalchemy import event as sa_event
    counters={"campaignEligible":0,"campaignIneligible":0,"claimGranted":0,"claimNone":0,
        "skipLockedEmpty":0,"skipLockedPositive":0,"skipLockedUnknown":0}
    def trace_claim_select(connection,cursor,statement,parameters,execution_context,executemany):
        # Observe the existing claim SELECT's DB-API rowcount. Never fetch from
        # its cursor or print SQL/parameters, so claim semantics stay untouched.
        normalized=" ".join(statement.upper().split())
        if "FOR UPDATE OF OF_RUNTIME SKIP LOCKED" in normalized:
            key="skipLockedEmpty" if cursor.rowcount==0 else "skipLockedPositive" if cursor.rowcount>0 else "skipLockedUnknown"
            counters[key]+=1
    sa_event.listen(store.engine,"after_cursor_execute",trace_claim_select)
    try:
        runtime=worker(store,model=model,transport=wire)
        can_dispatch=runtime._can_dispatch_campaign
        def traced_can_dispatch(account):
            result=can_dispatch(account)
            counters["campaignEligible" if result else "campaignIneligible"]+=1
            return result
        runtime._can_dispatch_campaign=traced_can_dispatch
        claim=store.claim
        def traced_claim(*args,**kwargs):
            result=claim(*args,**kwargs)
            counters["claimNone" if result is None else "claimGranted"]+=1
            return result
        store.claim=traced_claim
        # Nonlocking fixture eligibility evidence before the start barrier; no
        # extra query is inserted into the competing claim loop itself.
        with Session(store.engine) as s:
            initial=dict(s.execute(select(OfWork.status,OfWork.due_at,
                (OfWork.due_at<=func.clock_timestamp()).label("due"),
                OfRuntime.enabled.label("runtime_enabled"),OfRuntime.recovery_needed,OfRuntime.pending_events,
                OfRuntime.last_sync_at,OfDeliveryPolicy.enabled.label("policy_enabled"),
                OfDeliveryPolicy.transport_kind,OfDeliveryPolicy.used_units,OfDeliveryPolicy.limit_units,
                OfDeliveryPolicy.queued_count,OfDeliveryPolicy.next_dispatch_at,
                OfConversationState.mode,OfConversationState.takeover_state)
                .select_from(OfWork).join(OfRuntime,OfRuntime.account_id==OfWork.account_id)
                .join(OfDeliveryPolicy,OfDeliveryPolicy.account_id==OfWork.account_id)
                .join(OfConversationState,(OfConversationState.account_id==OfWork.account_id)&
                    (OfConversationState.fan_id==OfWork.fan_id))
                .where(OfWork.account_id==ACCOUNT,OfWork.fan_id==101)).mappings().one())
            initial={key:value.isoformat() if hasattr(value,"isoformat") else value for key,value in initial.items()}
        ready.put(True)
        assert gate.wait(30),"parent did not release both ready workers"
        reports=[]
        def observe(report):
            reports.append(report)
            # step() has committed its accepted receipt before reporting it.
            if report["accepted"]==1:done.set()
        runtime.run(done,max_cycles=200,poll_seconds=.05,observe=observe)
        # Both competitors retain the original five-cycle replay check after
        # acceptance; the completion signal must not hide a late duplicate.
        runtime.run(threading.Event(),max_cycles=5,poll_seconds=.05,observe=reports.append)
        output.put((len(model.calls),len(wire.calls),reports,{"counters":counters,"initial":initial}))
    finally:
        sa_event.remove(store.engine,"after_cursor_execute",trace_claim_select)
        store.close()


def test_two_full_worker_processes_have_one_native_invocation_and_one_send(database,monkeypatch):
    store,_=start(database,monkeypatch);context=multiprocessing.get_context("spawn")
    ready=context.Queue();gate=context.Event();done=context.Event();output=context.Queue()
    children=[context.Process(target=compete_worker,args=(database[0],ready,gate,done,output)) for _ in range(2)]
    try:
        for child in children:child.start()
        for _ in children:assert ready.get(timeout=20) is True
        gate.set()
        try:
            accepted_completion=done.wait(10)
        finally:
            # Preserve whether acceptance actually occurred before parent cleanup.
            done.set()
        # Drain reports before joining so a full diagnostic queue cannot block a
        # child's feeder thread on the timeout path.
        values=[output.get(timeout=20) for _ in children]
        for child in children:child.join(20);assert child.exitcode==0
        # Synthetic counters and durable state only: no bodies, prompts, tokens,
        # credentials or receipt payloads. Preserve the exact-once counts.
        with Session(database[1]) as s:
            runtime=s.get(OfRuntime,ACCOUNT)
            policy=s.get(OfDeliveryPolicy,ACCOUNT)
            diagnostic={
                "acceptedCompletion":accepted_completion,
                "children":[{"modelCalls":row[0],"sendCalls":row[1],"cycles":row[2],"trace":row[3]} for row in values],
                "work":[dict(row) for row in s.execute(select(OfWork.status,OfWork.generation,
                    OfWork.control_revision,OfWork.target_message_id)).mappings()],
                "plans":[dict(row) for row in s.execute(select(OfNativePlan.status,OfNativePlan.reason,
                    OfNativePlan.model_call_id)).mappings()],
                "intents":[dict(row) for row in s.execute(select(OfTextIntent.status,OfTextIntent.reason)).mappings()],
                "runtime":{"enabled":runtime.enabled,"recoveryNeeded":runtime.recovery_needed,
                    "pendingEvents":runtime.pending_events},
                "policy":{"enabled":policy.enabled,"usedUnits":policy.used_units,
                    "queuedCount":policy.queued_count,"activeIntent":bool(policy.active_intent_id)},
            }
            detail=json.dumps(diagnostic,sort_keys=True)
            print("synthetic_worker_race_diagnostic="+detail,flush=True)
            assert accepted_completion,"no committed acceptance within 10 seconds: "+detail
            assert sum(row[0] for row in values)==sum(row[1] for row in values)==1,detail
            assert s.scalar(select(func.count()).select_from(GrokCall))==1,detail
            assert s.scalar(select(func.count()).select_from(Message).where(Message.direction=="out"))==1,detail
    finally:
        done.set();gate.set()
        for child in children:
            if child.pid is not None:
                child.join(20)
                if child.is_alive():child.kill();child.join(3)
        store.close()


def test_database_outage_backoff_recovers_without_a_speculative_model_call(database,monkeypatch):
    store,_=start(database,monkeypatch);model=SyntheticNativeModel();runtime=worker(store,model=model);stop=threading.Event();reports=[]
    admin=create_engine(os.environ["ONLYFANS_TEST_PG_URL"],isolation_level="AUTOCOMMIT");name=database[1].url.database
    try:
        with admin.connect() as c:
            c.exec_driver_sql(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS false')
            c.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=:name"),{"name":name})
        def observe(value):
            reports.append(value)
            if value["errors"]:
                assert model.calls==[]
                with admin.connect() as c:c.exec_driver_sql(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS true')
                database[1].dispose()
            if value["accepted"]:stop.set()
        assert runtime.run(stop,max_cycles=3,poll_seconds=.05,max_backoff_seconds=.1,observe=observe)==2
        assert reports[0]["errors"]==1 and reports[1]["accepted"]==1 and len(model.calls)==1
    finally:
        with admin.connect() as c:c.exec_driver_sql(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS true')
        admin.dispose();database[1].dispose();store.close()


def test_round_robin_account_window_progresses_without_cross_account_identity_collision(database,monkeypatch):
    store,_=start(database,monkeypatch);accounts=[ACCOUNT]+[f"rotate-{i}" for i in range(4)]
    for account in accounts[1:]:
        another=seed(*database,account=account)
        with Session(database[1]) as s,s.begin():
            runtime=s.get(OfRuntime,account);runtime.recovery_needed=False;runtime.last_sync_at=utcnow()
            s.add(AccountAiConfig(account_id=account,model="grok-4-1-fast-non-reasoning",persona="Synthetic creator enjoys hiking."))
        TextAuthority(another).configure(TENANT,account,expected_revision=0,enabled=True,limit_units=3,
            throttle_seconds=1,actor="synthetic",reason="Offline fair worker cohort")
        another.record(TENANT,account,event());another.drain(TENANT,account);another.close()
    due(database[1])
    bound=tuple(WorkerAccount(account,SyntheticNativeModel(),SyntheticTextTransport(account)) for account in accounts)
    runtime=NativeWorker(store,TENANT,bound,accounts_per_cycle=1)
    assert [runtime.step().accepted for _ in accounts]==[1]*5
    assert all(len(account.model.calls)==len(account.transport.calls)==1 for account in bound)
    with Session(database[1]) as s:
        assert s.scalar(select(func.count()).select_from(Message).where(Message.direction=="out",Message.message_id==9001))==5
    store.close()


def test_account_wide_fastt_scope_makes_the_worker_claim_plan_and_send_nothing(database,monkeypatch):
    from onlyfans_runtime.fastt_manual import SCOPE_ENV
    store,_=start(database,monkeypatch);model=SyntheticNativeModel(reported_cost_millicents=7)
    wire=SyntheticTextTransport(ACCOUNT)
    monkeypatch.setenv(SCOPE_ENV,f"{ACCOUNT}:*")
    runtime=worker(store,model=model,transport=wire)
    for _ in range(3):
        r=runtime.step();assert (r.claims,r.queued,r.accepted)==(0,0,0)
    assert not model.calls and not wire.calls
    with Session(database[1]) as s:
        assert s.scalar(select(func.count()).select_from(OfTextIntent))==0
    store.close()
