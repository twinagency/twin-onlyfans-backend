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


def compete_worker(url,gate,output):
    os.environ.update(ONLYFANS_PROCESS_ROLE="worker",DATABASE_URL=url)
    store=Store(url);model=SyntheticNativeModel(delay_seconds=.2,reported_cost_millicents=7);wire=SyntheticTextTransport(ACCOUNT)
    try:
        runtime=worker(store,model=model,transport=wire);gate.wait(10)
        runtime.run(threading.Event(),max_cycles=5,poll_seconds=.05)
        output.put((len(model.calls),len(wire.calls)))
    finally:store.close()


def test_two_full_worker_processes_have_one_native_invocation_and_one_send(database,monkeypatch):
    store,_=start(database,monkeypatch);context=multiprocessing.get_context("spawn");gate=context.Event();output=context.Queue()
    children=[context.Process(target=compete_worker,args=(database[0],gate,output)) for _ in range(2)]
    try:
        for child in children:child.start()
        gate.set()
        for child in children:child.join(20);assert child.exitcode==0
        values=[output.get(timeout=2) for _ in children]
        assert sum(row[0] for row in values)==sum(row[1] for row in values)==1
        with Session(database[1]) as s:
            assert s.scalar(select(func.count()).select_from(GrokCall))==1
            assert s.scalar(select(func.count()).select_from(Message).where(Message.direction=="out"))==1
    finally:
        for child in children:
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


# --- Temporary pre-model history holds recover without replay -------------------------------------------------
from test_onlyfans_text_delivery_pg import FAN
from db.onlyfans_models import OfIngress,OfRecovery


def held_by_history(database,monkeypatch,*,model=None):
    """A claimed input refused before any model call because this fan's own history is not current."""
    monkeypatch.setenv("ONLYFANS_FAN_CURRENT_OVERRIDE",f"{ACCOUNT}:{FAN}")
    store,_=start(database,monkeypatch)
    with Session(database[1]) as s,s.begin():
        runtime=s.get(OfRuntime,ACCOUNT);runtime.recovery_needed=True;runtime.last_sync_at=None
    store.need_recovery(TENANT,ACCOUNT,FAN)
    model=model or SyntheticNativeModel(reported_cost_millicents=7)
    runner=worker(store,model=model)
    report=runner.step()
    assert (report.claims,report.held,report.queued)==(1,1,0)
    with Session(database[1]) as s:
        work=s.get(OfWork,(ACCOUNT,FAN))
        assert (work.status,work.block_reason,work.block_phase)==("blocked","history_not_current","before_model")
        assert s.scalar(select(func.count()).select_from(OfNativePlan))==0 and not model.calls
    return store,runner,model


def complete_fan_recovery(store):
    claim=store.claim_recovery(TENANT,ACCOUNT)
    assert claim is not None and claim.fan_id==FAN
    assert store.recovery_page(TENANT,claim,[],has_more=False)=="complete"


def counts(database):
    with Session(database[1]) as s:
        return (s.scalar(select(func.count()).select_from(OfNativePlan)),s.scalar(select(func.count()).select_from(OfTextIntent)),
            s.scalar(select(func.count()).select_from(OfTextIntent).where(OfTextIntent.status=="accepted")))


def test_temporary_history_hold_clears_and_the_same_input_is_answered_once(database,monkeypatch):
    store,runner,model=held_by_history(database,monkeypatch)
    # Still not current: stepping keeps the hold and makes no model call.
    for _ in range(2):assert runner.step().claims==0
    assert counts(database)==(0,0,0) and not model.calls
    complete_fan_recovery(store)
    first=runner.step()
    assert (first.claims,first.queued,first.accepted,first.errors)==(1,1,1,0)
    for _ in range(3):assert runner.step().claims==0
    assert counts(database)==(1,1,1) and len(model.calls)==1
    with Session(database[1]) as s:
        work=s.get(OfWork,(ACCOUNT,FAN));assert work.block_reason is None and work.block_phase is None
        assert s.scalar(select(func.count()).select_from(Message).where(Message.direction=="out"))==1
        assert s.get(OfDeliveryPolicy,ACCOUNT).used_units==1
    store.close()


def plan_count_for_new_revision(database):
    with Session(database[1]) as s:
        return [(row.input_revision,row.status) for row in s.scalars(select(OfNativePlan).order_by(OfNativePlan.input_revision))]


@pytest.mark.parametrize("change",["pause","takeover"])
def test_control_change_during_a_history_hold_is_never_overridden(database,monkeypatch,change):
    store,runner,model=held_by_history(database,monkeypatch)
    with Session(database[1]) as s:revision=s.get(OfConversationState,(ACCOUNT,FAN)).revision
    action,mode=("pause_automation","paused") if change=="pause" else ("request_takeover","human_controlled")
    store.control(TENANT,ACCOUNT,FAN,mode=mode,expected_revision=revision,command_key="c-"+change,actor="synthetic",
        reason="operator decision",action=action)
    complete_fan_recovery(store)
    assert store.reconcile_blocked(TENANT,ACCOUNT)==0
    for _ in range(3):assert runner.step().claims==0
    assert counts(database)==(0,0,0) and not model.calls
    with Session(database[1]) as s:assert s.get(OfWork,(ACCOUNT,FAN)).status=="blocked"
    store.close()


def test_newer_input_after_a_history_hold_is_answered_once_on_its_own_revision(database,monkeypatch):
    store,runner,model=held_by_history(database,monkeypatch)
    store.record(TENANT,ACCOUNT,event(mid=2,body="Are you still there?"));store.drain(TENANT,ACCOUNT);due(database[1])
    complete_fan_recovery(store)
    assert store.reconcile_blocked(TENANT,ACCOUNT)==0  # _apply already re-waited it; nothing stale to wake
    result=runner.step()
    assert (result.claims,result.accepted)==(1,1)
    for _ in range(3):assert runner.step().claims==0
    assert counts(database)==(1,1,1) and len(model.calls)==1
    assert "Are you still there?" in json.dumps(model.calls[0])
    store.close()


def test_stale_binding_during_a_history_hold_sends_nothing(database,monkeypatch):
    store,runner,model=held_by_history(database,monkeypatch)
    complete_fan_recovery(store)
    with Session(database[1]) as s,s.begin():
        s.get(OfCreatorBinding,"binding-"+ACCOUNT).revoked_at=utcnow()
    with pytest.raises(Refused):store.reconcile_blocked(TENANT,ACCOUNT)
    for _ in range(2):runner.step()
    assert counts(database)==(0,0,0) and not model.calls
    with Session(database[1]) as s:assert s.get(OfWork,(ACCOUNT,FAN)).status=="blocked"
    store.close()


def test_quarantined_ingress_keeps_a_history_hold(database,monkeypatch):
    store,runner,model=held_by_history(database,monkeypatch)
    complete_fan_recovery(store)
    with Session(database[1]) as s,s.begin():
        s.add(OfIngress(account_id=ACCOUNT,binding_id="binding-"+ACCOUNT,event_key="q"*64,fan_id=FAN,message_id=77,
            source="rest",payload="{}",status="quarantined",reason="conflicting_message_identity",received_at=utcnow()))
    assert store.reconcile_blocked(TENANT,ACCOUNT)==0
    for _ in range(2):assert runner.step().claims==0
    assert counts(database)==(0,0,0) and not model.calls
    store.close()


def test_uncertain_model_operation_is_never_resumed_or_replayed(database,monkeypatch):
    store,_=start(database,monkeypatch)
    model=SyntheticNativeModel(fail=True,reported_cost_millicents=7);runner=worker(store,model=model)
    assert runner.step().claims==1
    with Session(database[1]) as s:
        assert [p.status for p in s.scalars(select(OfNativePlan))]==["unknown"]
        assert s.get(OfWork,(ACCOUNT,FAN)).status=="blocked"
    # Even if the block were mislabelled as a retryable pre-model hold, the plan row for this input revision wins.
    with Session(database[1]) as s,s.begin():
        work=s.get(OfWork,(ACCOUNT,FAN));state=s.get(OfConversationState,(ACCOUNT,FAN))
        work.block_reason,work.block_phase,work.block_input_revision="history_not_current","before_model",state.input_revision
    assert store.reconcile_blocked(TENANT,ACCOUNT)==0
    for _ in range(3):assert runner.step().claims==0
    assert len(model.calls)==1 and counts(database)==(1,0,0)
    store.close()


def test_started_dispatch_keeps_its_hold(database,monkeypatch):
    store,_=start(database,monkeypatch)
    model=SyntheticNativeModel(reported_cost_millicents=7)
    runner=worker(store,model=model)
    assert runner.step().accepted==1
    # A later retryable-looking hold on the same revision is not woken: a plan and intent already exist.
    with Session(database[1]) as s,s.begin():
        work=s.get(OfWork,(ACCOUNT,FAN));state=s.get(OfConversationState,(ACCOUNT,FAN))
        work.status,work.block_reason,work.block_phase,work.block_input_revision="blocked","history_not_current","before_model",state.input_revision
    assert store.reconcile_blocked(TENANT,ACCOUNT)==0
    assert counts(database)==(1,1,1) and len(model.calls)==1
    store.close()


def test_only_retryable_pre_model_reasons_are_woken(database,monkeypatch):
    store,runner,model=held_by_history(database,monkeypatch)
    complete_fan_recovery(store)
    for reason,phase in (("refused","before_model"),("history_not_current","model_started"),("history_not_current",None)):
        with Session(database[1]) as s,s.begin():
            work=s.get(OfWork,(ACCOUNT,FAN));work.block_reason,work.block_phase=reason,phase
        assert store.reconcile_blocked(TENANT,ACCOUNT)==0
    with Session(database[1]) as s,s.begin():
        work=s.get(OfWork,(ACCOUNT,FAN));work.block_reason,work.block_phase="history_not_current","before_model"
    assert store.reconcile_blocked(TENANT,ACCOUNT)==1
    with Session(database[1]) as s:
        work=s.get(OfWork,(ACCOUNT,FAN));assert work.status=="waiting" and work.lease_token is None
    store.close()
