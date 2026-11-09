"""Provider canonical mapping must be authenticated and pinned before the exact call."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
import httpx
import pytest
from sqlalchemy import select,text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from db.onlyfans_engine_models import OfEngineModelCall
from onlyfans_runtime.launch_operations_models import OfLaunchCommand
from onlyfans_runtime.store import Refused
from model_transport.pilot_supervised import build_supervised_pilot
from model_transport.reconciliation import pin_model_canonical,reconcile_model_usage
from test_onlyfans_foundation_pg import database
from test_engine_real_worker_pg import world
from test_plan_c_draft_app_pg import configured_draft
from test_plan_c_supervised_pg import supervised,stage
from test_plan_c_scope_pg import ACTOR
from test_onlyfans_text_delivery_pg import TENANT,ACCOUNT
from test_offline_completion_pg import normal_pilot


def prepare(supervised, *, canonical="m/primary-20260910", bill_model=None, bill_id="gen-pinned",
            pin=True, mapping_status=200, barrier=None):
    w,env,_,_,_=supervised
    normal_pilot(w,env,selecting=False)
    state={"canonical":canonical,"bill_model":bill_model or canonical,"bill_id":bill_id,
           "mapping_status":mapping_status,"mapping_id":"m/primary","posts":[],"mapping_reads":[],"billing_reads":[]}
    def wire(req):
        if req.method=="GET" and req.url.path.endswith("/model/m/primary"):
            state["mapping_reads"].append(req)
            if barrier:barrier.wait(timeout=20)
            if state.get("mapping_callback"):state["mapping_callback"]()
            if state.get("duplicate_mapping"):
                return httpx.Response(200,content=b'{"data":{"id":"m/primary","canonical_slug":"m/first","canonical_slug":"m/second"}}')
            return httpx.Response(state["mapping_status"],json={"data":{"id":state["mapping_id"],"canonical_slug":state["canonical"]}})
        if req.method=="POST" and req.url.path.endswith("/chat/completions"):
            state["posts"].append(req)
            return httpx.Response(200,json={"id":"gen-pinned","error":{"message":"synthetic incomplete generation"}})
        if req.method=="GET" and req.url.path.endswith("/generation"):
            state["billing_reads"].append(req)
            assert req.url.params["id"]=="gen-pinned"
            return httpx.Response(200,json={"data":{"id":state["bill_id"],"model":state["bill_model"],
                "total_cost":0.0001,"finish_reason":"error","provider_name":"Fixture Provider"}})
        raise AssertionError("unexpected external operation")
    pilot=build_supervised_pilot(w.store,env,model_transport=httpx.MockTransport(wire),fans_transport=w.fans.transport)
    runtime=pilot.worker.engine.model
    common=dict(tenant=TENANT,account=ACCOUNT,actor=ACTOR,expected_pilot_revision=pilot.scope_revision,
                reason="verify provider canonical mapping",environ=env)
    if pin:
        pin_model_canonical(w.store,runtime.config,runtime.client,**common)
    stage(pilot,24500,"synthetic hello")
    with Session(w.database[1]) as s:
        row=s.scalar(select(OfEngineModelCall))
        assert row.status=="unknown" and row.provider_generation_id=="gen-pinned"
        call_id,day,reserved=row.call_id,row.cost_day,row.reserved_microusd
    args={**common,"call_id":call_id,"command_key":"c1"*32,"reason":"reconcile exact provider generation"}
    return w,pilot,runtime,state,common,args,day,reserved


@pytest.mark.parametrize("supervised",["autonomous"],indirect=True)
def test_canonical_match_is_pinned_before_call_and_audited_without_authority(supervised):
    w,pilot,runtime,state,common,args,day,reserved=prepare(supervised)
    result=reconcile_model_usage(w.store,runtime.config,runtime.client,**args)
    assert result["actualMicrousd"]==100 and result["outcomeStillUnknown"]
    assert not result["sendAuthorized"] and not result["retryAuthorized"]
    assert reconcile_model_usage(w.store,runtime.config,runtime.client,**args)["replayed"]
    assert len(state["posts"])==len(state["mapping_reads"])==len(state["billing_reads"])==1
    with Session(w.database[1]) as s:
        row=s.get(OfEngineModelCall,(ACCOUNT,args["call_id"]))
        assert row.status=="unknown" and row.usage_known and row.outcome=="provider_incomplete"
        audit=s.scalar(select(OfLaunchCommand).where(OfLaunchCommand.kind=="plan_c_model_cost_reconcile"))
        before=json.loads(audit.before_json)
        assert before["requestedModel"]=="m/primary"
        assert before["providerReportedModel"]=="m/primary-20260910"
        assert before["providerEvidence"]["generationId"]=="gen-pinned"
        pin=before["canonicalMappingEvidence"]
        assert pin["requestedModel"]=="m/primary" and pin["canonicalModel"]=="m/primary-20260910"
        assert len(pin["mappingReceiptSha256"])==64 and pin["callId"]==args["call_id"]
    with w.database[1].begin() as c:
        with pytest.raises(DBAPIError):
            with c.begin_nested():c.execute(text("UPDATE of_launch_command SET before_json='{}' WHERE kind='plan_c_model_call_pin'"))
    for _ in range(3):pilot.tick()
    assert len(state["posts"])==1 and w.fans.posts==[]


@pytest.mark.parametrize("supervised",["autonomous"],indirect=True)
@pytest.mark.parametrize("case",["wrong_model","wrong_generation","changed_alias"])
def test_conflicting_generation_or_changed_alias_keeps_reservation(supervised,case):
    w,pilot,runtime,state,common,args,day,reserved=prepare(supervised)
    if case=="wrong_generation":state["bill_id"]="gen-other"
    else:state["bill_model"]="m/other-20260910"
    if case=="changed_alias":state["canonical"]="m/other-20260910"
    with pytest.raises(Refused):reconcile_model_usage(w.store,runtime.config,runtime.client,**args)
    assert runtime.ledger.committed(day=day,account=ACCOUNT)==reserved
    with Session(w.database[1]) as s:
        row=s.get(OfEngineModelCall,(ACCOUNT,args["call_id"]))
        assert row.status=="unknown" and not row.usage_known
        assert not list(s.scalars(select(OfLaunchCommand).where(OfLaunchCommand.kind=="plan_c_model_cost_reconcile")))
    assert len(state["mapping_reads"])==1 and len(state["posts"])==1 and w.fans.posts==[]


@pytest.mark.parametrize("supervised",["autonomous"],indirect=True)
def test_late_or_unavailable_mapping_cannot_retrofit_an_old_call(supervised):
    w,pilot,runtime,state,common,args,day,reserved=prepare(supervised,pin=False,mapping_status=503)
    with pytest.raises(Refused,match="mapping is unavailable"):
        pin_model_canonical(w.store,runtime.config,runtime.client,**common)
    state["mapping_status"]=200
    assert pin_model_canonical(w.store,runtime.config,runtime.client,**common)["ok"]
    with pytest.raises(Refused,match="does not match"):
        reconcile_model_usage(w.store,runtime.config,runtime.client,**args)
    assert runtime.ledger.committed(day=day,account=ACCOUNT)==reserved
    assert len(state["posts"])==1 and w.fans.posts==[]


@pytest.mark.parametrize("supervised",["autonomous"],indirect=True)
def test_concurrent_pinned_reconciliation_is_idempotent(supervised):
    w,pilot,runtime,state,common,args,day,reserved=prepare(supervised)
    assert pin_model_canonical(w.store,runtime.config,runtime.client,**common)["replayed"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:reconcile_model_usage(w.store,runtime.config,runtime.client,**args),range(2)))
    assert sorted(x["replayed"] for x in results)==[False,True]
    assert all(x["outcomeStillUnknown"] and not x["sendAuthorized"] and not x["retryAuthorized"] for x in results)
    assert runtime.ledger.committed(day=day,account=ACCOUNT)==100 and len(state["posts"])==1 and w.fans.posts==[]


@pytest.mark.parametrize("supervised",["autonomous"],indirect=True)
@pytest.mark.parametrize("case",["wrong_alias","duplicate_fields","missing_canonical"])
def test_provider_mapping_must_be_exact_and_unambiguous(supervised,case):
    w,pilot,runtime,state,common,args,day,reserved=prepare(supervised,pin=False)
    if case=="wrong_alias":state["mapping_id"]="m/other"
    elif case=="duplicate_fields":state["duplicate_mapping"]=True
    else:state["canonical"]=None
    with pytest.raises(Refused,match="mapping is incomplete or conflicting"):
        pin_model_canonical(w.store,runtime.config,runtime.client,**common)
    with pytest.raises(Refused,match="does not match"):
        reconcile_model_usage(w.store,runtime.config,runtime.client,**args)
    with Session(w.database[1]) as s:
        assert not list(s.scalars(select(OfLaunchCommand).where(OfLaunchCommand.kind=="plan_c_model_canonical_pin")))
    assert runtime.ledger.committed(day=day,account=ACCOUNT)==reserved
    assert len(state["posts"])==1 and w.fans.posts==[]


@pytest.mark.parametrize("supervised",["autonomous"],indirect=True)
def test_scope_change_during_mapping_read_cannot_create_pin(supervised):
    from conversation_engine import pilot_scope
    w,pilot,runtime,state,common,args,day,reserved=prepare(supervised,pin=False)
    def stop():
        state["mapping_callback"]=None
        pilot_scope.set_status(w.store,TENANT,ACCOUNT,"stopped",
            expected_revision=pilot.scope_revision,command_key="c2"*32,actor=ACTOR,
            reason="scope changed during mapping read")
    state["mapping_callback"]=stop
    with pytest.raises(Refused,match="revision changed"):
        pin_model_canonical(w.store,runtime.config,runtime.client,**common)
    with Session(w.database[1]) as s:
        assert not list(s.scalars(select(OfLaunchCommand).where(OfLaunchCommand.kind=="plan_c_model_canonical_pin")))
    assert runtime.ledger.committed(day=day,account=ACCOUNT)==reserved and w.fans.posts==[]


@pytest.mark.parametrize("supervised",["autonomous"],indirect=True)
def test_concurrent_mapping_pin_is_one_immutable_record_and_not_retroactive(supervised):
    barrier=threading.Barrier(2)
    w,pilot,runtime,state,common,args,day,reserved=prepare(supervised,pin=False,barrier=barrier)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:pin_model_canonical(w.store,runtime.config,runtime.client,**common),range(2)))
    assert sorted(x["replayed"] for x in results)==[False,True]
    with Session(w.database[1]) as s:
        assert len(list(s.scalars(select(OfLaunchCommand).where(OfLaunchCommand.kind=="plan_c_model_canonical_pin"))))==1
        assert not list(s.scalars(select(OfLaunchCommand).where(OfLaunchCommand.kind=="plan_c_model_call_pin")))
    with pytest.raises(Refused,match="does not match"):
        reconcile_model_usage(w.store,runtime.config,runtime.client,**args)
    assert runtime.ledger.committed(day=day,account=ACCOUNT)==reserved and w.fans.posts==[]


@pytest.mark.parametrize("supervised",["autonomous"],indirect=True)
def test_old_call_uses_its_pinned_mapping_even_if_current_alias_changes(supervised):
    w,pilot,runtime,state,common,args,day,reserved=prepare(supervised)
    # The provider's current catalogue later points elsewhere; this call's
    # immutable pre-reservation pin and generation record still agree.
    state["canonical"]="m/primary-newer"
    result=reconcile_model_usage(w.store,runtime.config,runtime.client,**args)
    assert result["actualMicrousd"]==100 and result["outcomeStillUnknown"]
    with Session(w.database[1]) as s:
        audit=s.scalar(select(OfLaunchCommand).where(OfLaunchCommand.kind=="plan_c_model_cost_reconcile"))
        before=json.loads(audit.before_json)
        assert before["canonicalMappingEvidence"]["canonicalModel"]=="m/primary-20260910"
        assert before["providerEvidence"]["model"]=="m/primary-20260910"
    assert len(state["mapping_reads"])==1 and len(state["posts"])==1 and w.fans.posts==[]
