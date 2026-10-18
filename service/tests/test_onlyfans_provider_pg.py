"""Native adapter integration against disposable PostgreSQL and mocked HTTPS.

All credentials and response bodies are synthetic. No real account, provider,
model endpoint, production database or external request is used by this suite.
"""
from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, event, due
from test_onlyfans_worker_pg import start
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT
from test_onlyfans_provider_adapters import MODEL, OWNER, credential, expires, model
from onlyfans_native_protocol_fixture import native_message
from db.models import AccountAiConfig, GrokCall, GrokDailyCost, Message, OfConversationState
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfNativePlan, OfTextIntent
from onlyfans_runtime.native_worker import NativeWorker, WorkerAccount
from onlyfans_runtime.native_planning import NativeTextPlanner, SyntheticNativeModel
from onlyfans_runtime.provider_connector import NativeProviderConnector
from onlyfans_runtime.provider_model import NativeProviderModel
from onlyfans_runtime.store import Refused
from onlyfans_runtime.text_delivery import SyntheticTextTransport


def native_start(database, monkeypatch):
    store,authority=start(database,monkeypatch)
    assert authority.configure(TENANT,ACCOUNT,expected_revision=1,enabled=True,limit_units=3,
        throttle_seconds=1,transport_kind="native",actor="synthetic-native-test",reason="Mocked HTTPS only")==2
    return store,authority


def completion(*, usage=True, body="Thanks for asking. How has your day been?", **overrides):
    result={"choices":[{"finish_reason":"stop","message":{"role":"assistant","content":json.dumps(
        {"reply":body,"facts":[],"profile":None})}}]}
    if usage:result["usage"]=usage if type(usage) is dict else {"prompt_tokens":1000,"completion_tokens":40}
    result.update(overrides)
    return json.dumps(result).encode()


def fake_requests(database, monkeypatch, *, output=None, failure=None, wire_mode="accepted", before_post=None):
    calls,requests=[],[]
    engine=database[1]
    async def model_post(self, body):
        # The model boundary observes committed provenance and released locks.
        with engine.begin() as c:
            c.exec_driver_sql("SET LOCAL lock_timeout='100ms'")
            assert c.scalar(text("SELECT account_id FROM of_runtime WHERE account_id=:a FOR UPDATE NOWAIT"),{"a":ACCOUNT})==ACCOUNT
            row=c.execute(text("SELECT status,was_dry_run,endpoint,model FROM grok_calls WHERE account_id=:a ORDER BY id DESC LIMIT 1"),{"a":ACCOUNT}).mappings().one()
            assert row["status"]=="pending" and row["was_dry_run"] is False
            assert row["endpoint"]==self.endpoint and row["model"]==MODEL
            assert c.scalar(text("SELECT count(*) FROM of_native_plan WHERE model_call_id IS NOT NULL AND status='generating'"))==1
        calls.append(json.loads(body))
        if failure:raise failure
        return completion() if output is None else output
    def wire_call(self, method, path, body=b""):
        self._ready();self._handle.current()
        requests.append((method,path,body))
        if path=="/api2/v2/users/me":return json.dumps({"id":OWNER+int(wire_mode=="wrong_owner")}).encode()
        if method=="GET":raise AssertionError("unexpected history read in current fixture")
        with engine.begin() as c:
            c.exec_driver_sql("SET LOCAL lock_timeout='100ms'")
            c.execute(text("SELECT account_id FROM of_runtime WHERE account_id=:a FOR UPDATE NOWAIT"),{"a":ACCOUNT})
            row=c.execute(text("SELECT wire_started_at,status FROM of_text_intent ORDER BY created_at DESC LIMIT 1")).mappings().one()
            assert row["status"]=="dispatching" and row["wire_started_at"] is not None
        if before_post:before_post()
        if wire_mode=="timeout":raise TimeoutError("synthetic native POST uncertainty")
        data=json.loads(body)
        identity=8999+2*sum(kind=="POST" for kind,_,_ in requests)
        reply=native_message(identity,owner=OWNER,outgoing=True,text=data["text"],media=data["mediaFiles"],price=data["price"])
        reply["previews"]=data["previews"]
        if wire_mode=="wrong_text":reply["text"]="<p>Different unconfirmed message</p>"
        return json.dumps(reply).encode()
    monkeypatch.setattr(NativeProviderModel,"_post_once",model_post)
    monkeypatch.setattr(NativeProviderConnector,"_call",wire_call)
    return calls,requests


def runtime(store, *, adapter=None, wire=None):
    adapter=model() if adapter is None else adapter
    wire=NativeProviderConnector(credential()) if wire is None else wire
    return NativeWorker(store,TENANT,(WorkerAccount(ACCOUNT,adapter,wire,wire),))


def test_native_worker_commits_model_and_send_once_then_projects_actual_receipt(database,monkeypatch):
    store,authority=native_start(database,monkeypatch)
    calls,requests=fake_requests(database,monkeypatch)
    worker=runtime(store)
    result=worker.step()
    with Session(database[1]) as s:
        reason=s.scalar(select(OfNativePlan.reason))
    assert (result.claims,result.queued,result.accepted,result.errors)==(1,1,1,0), (result.public(),reason,len(calls))
    for _ in range(3):assert worker.step().claims==0
    assert len(calls)==1 and [method for method,_,_ in requests]==["GET","POST"]
    assert calls[0]["model"]==MODEL and calls[0]["max_tokens"]==4096
    assert requests[1][1]=="/api2/v2/chats/101/messages"
    with Session(database[1]) as s:
        call=s.scalar(select(GrokCall));plan=s.scalar(select(OfNativePlan));intent=s.scalar(select(OfTextIntent))
        assert call.status=="done" and not call.was_dry_run and call.tokens_in==1000 and call.tokens_out==40
        assert call.cost_cents==plan.charged_millicents==s.scalar(select(GrokDailyCost)).cost_cents
        assert plan.billing_provider=="grok" and plan.resolved_model_id==MODEL
        assert s.get(Message,(ACCOUNT,FAN,9001)).body=="Thanks for asking. How has your day been?"
        assert intent.evidence_ref.startswith("native-https:") and intent.projected_at
        assert s.get(OfDeliveryPolicy,ACCOUNT).transport_kind=="native" and s.get(OfDeliveryPolicy,ACCOUNT).used_units==1
    store.close()


def test_account_model_credential_is_reusable_for_distinct_inputs_without_replaying_prior_plan(database,monkeypatch):
    store,_=native_start(database,monkeypatch)
    calls,requests=fake_requests(database,monkeypatch)
    worker=runtime(store)
    assert worker.step().accepted==1
    store.record(TENANT,ACCOUNT,event(mid=9002,body="A second separate question."));store.drain(TENANT,ACCOUNT);due(database[1])
    with database[1].begin() as c:
        c.execute(text("UPDATE of_delivery_policy SET next_dispatch_at=CURRENT_TIMESTAMP-INTERVAL '1 second'"))
    assert worker.step().accepted==1
    assert worker.step().claims==0
    assert len(calls)==2 and sum(method=="POST" for method,_,_ in requests)==2
    with Session(database[1]) as s:
        assert s.scalar(select(func.count()).select_from(GrokCall))==2
        assert s.scalar(select(func.count()).select_from(OfNativePlan))==2
        assert s.scalar(select(func.count()).select_from(Message).where(Message.direction=="out"))==2
        assert s.get(Message,(ACCOUNT,FAN,9003)) is not None
    store.close()


@pytest.mark.parametrize("mode",["timeout","wrong_text","wrong_owner"])
def test_native_send_uncertainty_holds_without_second_model_or_post(database,monkeypatch,mode):
    store,_=native_start(database,monkeypatch);calls,requests=fake_requests(database,monkeypatch,wire_mode=mode)
    worker=runtime(store);assert worker.step().unknown==1
    for _ in range(3):worker.step()
    assert len(calls)==1 and sum(method=="POST" for method,_,_ in requests)==int(mode!="wrong_owner")
    with Session(database[1]) as s:
        intent=s.scalar(select(OfTextIntent));policy=s.get(OfDeliveryPolicy,ACCOUNT)
        assert intent.status=="unknown" and policy.active_intent_id==intent.intent_id and policy.used_units==1
        assert s.scalar(select(GrokCall)).status=="done"
        assert s.scalar(select(func.count()).select_from(Message).where(Message.direction=="out"))==0
    store.close()


@pytest.mark.parametrize("case",["timeout","missing_usage","malformed_usage","malformed_response","incomplete"])
def test_model_uncertainty_retains_cost_and_never_sends_or_regenerates(database,monkeypatch,case):
    store,_=native_start(database,monkeypatch)
    options={}
    if case=="timeout":options["failure"]=TimeoutError("synthetic model uncertainty")
    if case=="missing_usage":options["output"]=completion(usage=False)
    if case=="malformed_usage":options["output"]=completion(usage={"prompt_tokens":True})
    if case=="malformed_response":options["output"]=b'{"usage":NaN}'
    if case=="incomplete":options["output"]=completion(choices=[{"finish_reason":"length","message":{"role":"assistant","content":"partial"}}])
    calls,requests=fake_requests(database,monkeypatch,**options)
    worker=runtime(store);worker.step()
    for _ in range(3):worker.step()
    assert len(calls)==1 and requests==[]
    with Session(database[1]) as s:
        plan=s.scalar(select(OfNativePlan));call=s.scalar(select(GrokCall))
        assert plan.status in {"unknown","refused"}
        assert call.status==("done" if case=="incomplete" else "unknown")
        assert s.scalar(select(GrokDailyCost)).cost_cents>0
        assert s.scalar(select(func.count()).select_from(OfTextIntent))==0
    store.close()


def test_native_saved_model_mismatch_never_resolves_fallback_key_or_calls_provider(database,monkeypatch):
    store,_=native_start(database,monkeypatch)
    with Session(database[1]) as s,s.begin():s.get(AccountAiConfig,ACCOUNT).model="deepseek-v4-flash-non-thinking"
    calls,requests=fake_requests(database,monkeypatch)
    worker=runtime(store);worker.step()
    assert calls==requests==[]
    with Session(database[1]) as s:
        assert s.scalar(select(OfNativePlan)).status=="refused"
        assert s.scalar(select(func.count()).select_from(GrokCall))==0
    store.close()


def test_live_adapter_refuses_synthetic_policy_and_synthetic_adapter_refuses_native_policy(database,monkeypatch):
    store,authority=start(database,monkeypatch)
    calls,requests=fake_requests(database,monkeypatch)
    worker=runtime(store);assert worker.step().claims==0 and calls==requests==[]
    authority.configure(TENANT,ACCOUNT,expected_revision=1,enabled=True,limit_units=3,throttle_seconds=1,
        transport_kind="native",actor="synthetic-test",reason="Mock native mode")
    with pytest.raises(Refused,match="match the account delivery mode"):
        authority.dispatch(TENANT,ACCOUNT,"not-created",SyntheticTextTransport(ACCOUNT))
    with pytest.raises(Refused,match="selected together"):
        NativeWorker(store,TENANT,(WorkerAccount(ACCOUNT,SyntheticNativeModel(),NativeProviderConnector(credential())),))
    assert calls==requests==[]
    store.close()


def test_native_zero_daily_cap_blocks_paid_call_before_request(database,monkeypatch):
    store,_=native_start(database,monkeypatch)
    with Session(database[1]) as s,s.begin():s.get(AccountAiConfig,ACCOUNT).daily_cost_cap_cents=0
    calls,requests=fake_requests(database,monkeypatch)
    runtime(store).step()
    assert calls==requests==[]
    with Session(database[1]) as s:
        assert s.scalar(select(OfNativePlan)).reason=="model_budget_refused"
        assert s.scalar(select(func.count()).select_from(GrokCall))==0
    store.close()


def test_native_manual_delivery_runs_without_any_model_credential(database,monkeypatch):
    store,authority=native_start(database,monkeypatch)
    calls,requests=fake_requests(database,monkeypatch)
    control=store.control(TENANT,ACCOUNT,FAN,mode="human_controlled",expected_revision=0,
        command_key="native-manual-takeover",actor="synthetic-operator",reason="Manual-only native account")
    with Session(database[1]) as s:revision=s.get(OfConversationState,(ACCOUNT,FAN)).revision
    authority.prepare(TENANT,ACCOUNT,FAN,command_key="native-manual-message",expected_revision=revision,
        body="An operator wrote this reply.",origin="manual",actor="synthetic-operator")
    wire=NativeProviderConnector(credential())
    worker=NativeWorker(store,TENANT,(WorkerAccount(ACCOUNT,None,wire,wire),))
    result=worker.step()
    assert result.accepted==1 and result.claims==result.errors==0
    assert calls==[] and sum(method=="POST" for method,_,_ in requests)==1
    with Session(database[1]) as s:
        assert s.scalar(select(func.count()).select_from(GrokCall))==0
        assert s.get(Message,(ACCOUNT,FAN,9001)).body=="An operator wrote this reply."
    store.close()


def test_native_campaign_runs_without_model_credential_or_invocation(database,monkeypatch):
    import hashlib
    from onlyfans_runtime.campaigns import CampaignService
    store,_=native_start(database,monkeypatch)
    calls,requests=fake_requests(database,monkeypatch)
    # Leave the existing inbound in the canonical history; no model is claimed
    # and no fake model is substituted when its credential is absent.
    service=CampaignService(store)
    created=service.create(TENANT,ACCOUNT,command_key=hashlib.sha256(b"native-no-model").hexdigest(),
        actor="synthetic-operator",name="Native no-model campaign",body="An operator-authored campaign.",
        audience={"kind":"all_known"})
    wire=NativeProviderConnector(credential())
    worker=NativeWorker(store,TENANT,(WorkerAccount(ACCOUNT,None,wire,wire),))
    result=worker.step()
    assert result.accepted==1 and result.claims==result.errors==0
    assert calls==[] and sum(method=="POST" for method,_,_ in requests)==1
    assert service.detail(TENANT,ACCOUNT,created["campaignId"])["campaign"]["counts"]["accepted"]==1
    with Session(database[1]) as s:assert s.scalar(select(func.count()).select_from(GrokCall))==0
    store.close()


def test_saved_synthetic_plan_cannot_become_a_live_draft_after_mode_switch(database,monkeypatch):
    store,authority=start(database,monkeypatch)
    claim=store.claim(TENANT)
    identity=NativeTextPlanner(store).prepare(TENANT,claim,SyntheticNativeModel())
    authority.configure(TENANT,ACCOUNT,expected_revision=1,enabled=True,limit_units=3,throttle_seconds=1,
        transport_kind="native",actor="synthetic-test",reason="Explicit mode transition")
    calls,requests=fake_requests(database,monkeypatch)
    report=runtime(store).step()
    assert report.accepted==report.claims==0 and calls==requests==[]
    with Session(database[1]) as s:
        assert s.get(OfNativePlan,(ACCOUNT,identity)).status=="stale"
        assert s.scalar(select(func.count()).select_from(GrokCall))==1
        assert s.scalar(select(GrokCall)).was_dry_run
        assert s.scalar(select(func.count()).select_from(OfTextIntent))==0
    store.close()


def test_expired_committed_plan_cannot_spend_the_native_model_credential(database,monkeypatch):
    store,_=native_start(database,monkeypatch)
    calls,requests=fake_requests(database,monkeypatch)
    original=NativeProviderModel.generate
    async def expired(self,messages):
        with database[1].begin() as connection:
            connection.execute(text("UPDATE of_native_plan SET expires_at=CURRENT_TIMESTAMP-INTERVAL '1 second'"))
        return await original(self,messages)
    monkeypatch.setattr(NativeProviderModel,"generate",expired)
    worker=runtime(store);worker.step()
    assert calls==requests==[]
    with Session(database[1]) as s:
        assert s.scalar(select(OfNativePlan)).status=="stale"
        assert s.scalar(select(GrokCall)).status=="unknown"
        assert s.scalar(select(func.count()).select_from(OfTextIntent))==0
    store.close()
