"""Offline pilot event journeys; external provider/model are simulated."""
from datetime import datetime,timezone
import pytest
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from db.models import Message,OfConversationState
from db.onlyfans_delivery_models import OfDeliveryPolicy
from db.onlyfans_engine_models import OfFansConnection,OfFansWebhook
from db.onlyfans_fans_intake_models import OfFansProjection,OfFansLink,OfFansEvidence
from test_onlyfans_foundation_pg import database
from test_engine_real_worker_pg import world,PROVIDER,CREATOR
from test_plan_c_draft_app_pg import configured_draft
from test_plan_c_supervised_pg import supervised,stage
from test_onlyfans_text_delivery_pg import ACCOUNT,TENANT,FAN
from test_plan_c_scope_pg import ACTOR
from test_offline_completion_pg import normal_pilot
from test_plan_c_offline_operations_pg import http_event
from model_transport.pilot_supervised import build_supervised_pilot
from onlyfans_runtime.store import Refused

@pytest.mark.parametrize("supervised",["autonomous"],indirect=True)
@pytest.mark.parametrize("event",["accounts.otp_code_required","accounts.face_otp_required"])
def test_auth_challenge_is_durable_and_requires_verified_recovery(supervised,event):
 w,env,model,seen,pilot=supervised
 response,_=http_event(pilot,{"event":event,"account_id":PROVIDER,"payload":{"onlyfans_id":str(CREATOR)}},"challenge-one")
 assert response.status_code==202
 with Session(w.database[1]) as s:
  link=s.get(OfFansLink,(ACCOUNT,str(CREATOR)));policy=s.get(OfDeliveryPolicy,ACCOUNT)
  assert link.send_blocked and link.state=="auth_failed" and policy.enabled is False
  assert s.scalar(select(OfFansWebhook).where(OfFansWebhook.event_key=="challenge-one")) is not None
 bad={"event":"accounts.reconnected","account_id":PROVIDER,"payload":{"onlyfans_id":str(CREATOR),"state":"authenticated","latestAuthAttempt":{"success":False}}}
 assert http_event(pilot,bad,"challenge-reconnect-unverified")[0].status_code==202
 with Session(w.database[1]) as s:
  link=s.get(OfFansLink,(ACCOUNT,str(CREATOR)));assert link.send_blocked and link.state!="connected"
 good={"event":"accounts.reconnected","account_id":PROVIDER,"payload":{"onlyfans_id":str(CREATOR),"state":"authenticated","latestAuthAttempt":{"success":True}}}
 assert http_event(pilot,good,"challenge-reconnect-verified")[0].status_code==202
 with Session(w.database[1]) as s:
  link=s.get(OfFansLink,(ACCOUNT,str(CREATOR)));assert link.send_blocked and link.state!="connected"
 with pytest.raises(Refused):
  pilot.worker.intake.release_send_block(TENANT,ACCOUNT,str(CREATOR),actor=ACTOR,reason="signed event is not current proof")
 assert w.fans.posts==[]
 # A real verified recovery path remains an unfinished acceptance criterion.

@pytest.mark.parametrize("supervised",["autonomous"],indirect=True)
def test_subscription_events_preserve_distinction_from_photo_evidence(supervised):
 w,env,_,_,_=supervised
 pilot,seen,_,_=normal_pilot(w,env,selecting=False)
 stamp=datetime.now(timezone.utc).isoformat()
 for event,user in [("subscriptions.renewed",str(CREATOR)),("subscriptions.expired",str(FAN))]:
  body={"event":event,"account_id":PROVIDER,"payload":{"id":"sub-event","user_id":user,"fan":{"id":str(FAN)},"createdAt":stamp,"expiredAt":stamp}}
  assert http_event(pilot,body,"event-"+event)[0].status_code==202
  with Session(w.database[1]) as s:
   row=s.scalar(select(OfFansWebhook).where(OfFansWebhook.event_key=="event-"+event))
   assert row is not None
   projection=s.get(OfFansProjection,(ACCOUNT,row.epoch,row.event_key))
   assert projection.status=="projected"
   assert s.scalar(select(func.count()).select_from(OfFansEvidence))==0
 stage(pilot,22110,"hello");pilot.tick()
 assert len(w.fans.posts)==1 and "price" not in w.fans.posts[0]["body"]

@pytest.mark.parametrize("supervised",["autonomous"],indirect=True)
def test_exact_deleted_stop_preserves_control_and_receipt(supervised):
 w,env,_,_,_=supervised
 pilot,seen,_,_=normal_pilot(w,env,selecting=False)
 stage(pilot,22120,"stop messaging me")
 assert http_event(pilot,{"event":"messages.deleted","account_id":PROVIDER,"payload":{"id":"22120"}},"delete-stop")[0].status_code==202
 with Session(w.database[1]) as s:
  msg=s.get(Message,(ACCOUNT,FAN,22120));state=s.get(OfConversationState,(ACCOUNT,FAN))
  assert msg is not None and msg.is_unsent is True
  assert state.mode=="no_contact"
  assert s.scalar(select(OfFansWebhook).where(OfFansWebhook.event_key=="delete-stop")) is not None
 assert w.fans.posts==[]

@pytest.mark.parametrize("supervised",["autonomous"],indirect=True)
def test_unmapped_deleted_message_remains_held_and_blocks_sending(supervised):
 w,env,_,_,_=supervised
 pilot,seen,_,_=normal_pilot(w,env,selecting=False)
 response,_=http_event(pilot,{"event":"messages.deleted","account_id":PROVIDER,"payload":{"id":"22444"}},"unmapped-delete")
 assert response.status_code==202
 with Session(w.database[1]) as s:
  raw=s.scalar(select(OfFansWebhook).where(OfFansWebhook.event_key=="unmapped-delete"))
  assert raw is not None
  projection=s.get(OfFansProjection,(ACCOUNT,raw.epoch,raw.event_key))
  assert projection.status=="held"
 stage(pilot,22445,"hello")
 pilot.tick()
 assert w.fans.posts==[]

@pytest.mark.parametrize("supervised",["autonomous"],indirect=True)
def test_ambiguous_message_id_remains_held_for_both_fans(supervised):
 from test_engine_real_worker_pg import FAN2
 w,env,_,_,_=supervised
 pilot,_,_,_=normal_pilot(w,env,selecting=False)
 stage(pilot,22500,"hello",fan=FAN,key="amb-a")
 stage(pilot,22500,"hello",fan=FAN2,key="amb-b")
 response,_=http_event(pilot,{"event":"messages.deleted","account_id":PROVIDER,"payload":{"id":"22500"}},"ambiguous-delete")
 assert response.status_code==202
 with Session(w.database[1]) as s:
  raw=s.scalar(select(OfFansWebhook).where(OfFansWebhook.event_key=="ambiguous-delete"))
  row=s.get(OfFansProjection,(ACCOUNT,raw.epoch,raw.event_key))
  assert row.status=="held"
  assert not s.get(Message,(ACCOUNT,FAN,22500)).is_unsent
  assert not s.get(Message,(ACCOUNT,FAN2,22500)).is_unsent
 pilot.tick()
 assert w.fans.posts==[]

@pytest.mark.parametrize("supervised",["autonomous"],indirect=True)
def test_deleted_accepted_card_keeps_evidence_and_cannot_duplicate(supervised):
 from test_engine_real_worker_pg import unlock,tx
 from db.onlyfans_delivery_models import OfTextIntent
 from conversation_engine.offers import match
 w,env,_,_,_=supervised
 pilot,seen,_,_=normal_pilot(w,env,selecting=True)
 stage(pilot,22600,"got any pics?");pilot.tick()
 assert len(w.fans.posts)==1 and w.fans.posts[0]["body"]["price"]==3.0
 with Session(w.database[1]) as s:
  intent=s.scalar(select(OfTextIntent).where(OfTextIntent.status=="accepted",OfTextIntent.price_cents==300))
  assert intent is not None and intent.provider_message_id is not None
  mid=intent.provider_message_id
 assert http_event(pilot,unlock(mid,amount=3),"unlock-before-delete")[0].status_code==202
 assert http_event(pilot,tx("del-evidence-transaction",amount=3),"transaction-before-delete")[0].status_code==202
 with Session(w.database[1]) as s:
  evidence=sorted((x.evidence_id,x.kind,x.raw_sha256) for x in s.scalars(select(OfFansEvidence)))
  assert any(x[1]=="unlock" for x in evidence) and any(x[1]=="transaction" for x in evidence)
 assert http_event(pilot,{"event":"messages.deleted","account_id":PROVIDER,"payload":{"id":str(mid)}},"delete-card")[0].status_code==202
 with Session(w.database[1]) as s:
  m=s.get(Message,(ACCOUNT,FAN,mid));assert m.is_unsent
  assert sorted((x.evidence_id,x.kind,x.raw_sha256) for x in s.scalars(select(OfFansEvidence)))==evidence
  runtime=w.store._runtime(s,TENANT,ACCOUNT,lock=False)
  ref=match(s,runtime,FAN,"that photo",include_cards=True)
  assert ref["state"]=="incomplete"
  assert any(c["sendState"]=="accepted" and c["messageDeleted"] for c in ref["cards"])
 before=len(seen)
 held=stage(pilot,22601,"Can you send me that photo?")
 pilot.tick()
 assert held.get("reason")=="pilot_evidence_pending"
 assert len(seen)==before
 assert len([x for x in w.fans.posts if x["body"].get("price")==3.0])==1
 stage(pilot,22602,"How was your day?")
 pilot.tick()
 assert len(seen)>before
 assert "message deleted" in seen[-1]["messages"][-1]["content"]
 assert len([x for x in w.fans.posts if x["body"].get("price")==3.0])==1

@pytest.mark.parametrize("supervised",["autonomous"],indirect=True)
def test_conflicting_subscription_fan_identities_are_held(supervised):
 from test_engine_real_worker_pg import FAN2
 w,env,_,_,_=supervised
 pilot,_,_,_=normal_pilot(w,env,selecting=False)
 stamp=datetime.now(timezone.utc).isoformat()
 body={"event":"subscriptions.renewed","account_id":PROVIDER,"payload":{"id":"conflict","user_id":str(CREATOR),"fan":{"id":str(FAN)},"user":{"id":str(FAN2)},"createdAt":stamp}}
 assert http_event(pilot,body,"conflicting-sub")[0].status_code==202
 with Session(w.database[1]) as s:
  raw=s.scalar(select(OfFansWebhook).where(OfFansWebhook.event_key=="conflicting-sub"))
  row=s.get(OfFansProjection,(ACCOUNT,raw.epoch,raw.event_key))
  assert row.status=="held"
  assert s.scalar(select(func.count()).select_from(OfFansEvidence))==0
 stage(pilot,22700,"hello")
 pilot.tick()
 assert w.fans.posts==[]


@pytest.mark.parametrize("supervised",["autonomous"],indirect=True)
def test_exact_nonpilot_deletion_does_not_block_pilot_or_mutate_outside_fan(supervised):
    # Model historical evidence retained before the narrow pilot was installed.
    # No non-pilot action or permission is created by this setup.
    import hashlib
    from test_plan_c_draft_app_pg import signed
    from test_engine_real_worker_pg import inbound
    from onlyfans_runtime.store import Event, utcnow
    w,env,_,_,_=supervised
    pilot,seen,_,_=normal_pilot(w,env,selecting=False)
    outside=103
    mid=23001
    raw,_=signed(inbound(mid,"historical outside pilot",fan=outside),key="outside-historical")
    evt=Event(outside,mid,datetime.now(timezone.utc).isoformat(),"in","historical outside pilot",(),0)
    w.store.record(TENANT,ACCOUNT,evt)
    w.store.drain(TENANT,ACCOUNT)
    with Session(w.database[1]) as s, s.begin():
        s.add(OfFansWebhook(account_id=ACCOUNT,epoch=1,event_key="outside-historical",
            event_type="messages.received",payload_sha256=hashlib.sha256(raw).hexdigest(),
            raw_body=raw,status="accepted",received_at=utcnow()))
        s.flush()
        s.add(OfFansProjection(account_id=ACCOUNT,epoch=1,event_key="outside-historical",
            status="projected",detail="historical",attempts=1,updated_at=utcnow()))
    response,_=http_event(pilot,{"event":"messages.deleted","account_id":PROVIDER,"payload":{"id":str(mid)}},"outside-delete")
    assert response.status_code==202
    with Session(w.database[1]) as s:
        receipt=s.scalar(select(OfFansWebhook).where(OfFansWebhook.event_key=="outside-delete"))
        projection=s.get(OfFansProjection,(ACCOUNT,receipt.epoch,receipt.event_key))
        assert projection.status=="ignored" and projection.detail=="deletion_outside_pilot"
        assert not s.get(Message,(ACCOUNT,outside,mid)).is_unsent
    stage(pilot,23002,"hello");pilot.tick()
    assert len(w.fans.posts)==1


@pytest.mark.parametrize("supervised",["autonomous"],indirect=True)
def test_deleted_source_during_generation_does_not_dispatch(supervised):
    w,env,_,_,_=supervised
    holder={}
    def during_model():
        response,_=http_event(holder["pilot"],{"event":"messages.deleted","account_id":PROVIDER,
            "payload":{"id":"23400"}},"delete-during-model")
        assert response.status_code==202
    pilot,seen,_,_=normal_pilot(w,env,selecting=False,on_call=during_model)
    holder["pilot"]=pilot
    stage(pilot,23400,"hello")
    pilot.tick()
    assert len(seen)==1 and w.fans.posts==[]
    with Session(w.database[1]) as s:
        assert s.get(Message,(ACCOUNT,FAN,23400)).is_unsent


@pytest.mark.parametrize("supervised",["autonomous"],indirect=True)
def test_deleted_source_after_dispatch_grant_prevents_provider_call(supervised,monkeypatch):
    from fastapi.testclient import TestClient
    from plan_c_worker import create_app
    from test_plan_c_draft_app_pg import signed
    from onlyfans_runtime.text_delivery import TextAuthority
    from onlyfans_runtime.store import Refused
    from conversation_engine import approval
    w,env,_,_,_=supervised
    pilot,_,_,_=normal_pilot(w,env,selecting=False)
    staged=stage(pilot,23410,"hello")
    info=staged["approval"]
    approval.authorize_automatic(w.store,TENANT,ACCOUNT,info["requestId"],info["generation"],
        expected_version=info["version"])
    original=TextAuthority.consume_dispatch
    reached=[]
    with TestClient(create_app(pilot,run_loop=False)) as client:
        def interleaved(self,tenant,grant,**kwargs):
            if not reached:
                reached.append(grant)
                raw,headers=signed({"event":"messages.deleted","account_id":PROVIDER,
                    "payload":{"id":"23410"}},key="delete-after-dispatch-grant")
                assert client.post("/onlyfans/webhooks/plan-c",content=raw,headers=headers).status_code==202
            return original(self,tenant,grant,**kwargs)
        monkeypatch.setattr(TextAuthority,"consume_dispatch",interleaved)
        try:
            pilot.worker.send_approved(TENANT,ACCOUNT,staged["decisionId"])
        except Refused:
            pass
    assert reached and w.fans.posts==[]
    with Session(w.database[1]) as s:
        assert s.get(Message,(ACCOUNT,FAN,23410)).is_unsent


@pytest.mark.parametrize("supervised",["autonomous"],indirect=True)
def test_deleted_source_after_wire_unknown_stays_unknown_without_retry(supervised):
    import httpx
    from test_engine_real_worker_pg import answer
    from db.onlyfans_delivery_models import OfTextIntent
    w,env,_,_,_=supervised
    base,_,_,model=normal_pilot(w,env,selecting=False)
    holder={}
    posts=[]
    def provider(req):
        if req.method=="POST" and req.url.path.endswith("/messages"):
            posts.append(req.url.path)
            response,_=http_event(holder["pilot"],{"event":"messages.deleted","account_id":PROVIDER,
                "payload":{"id":"23420"}},"delete-after-wire")
            assert response.status_code==202
            return answer(503,{"error":"ambiguous"})
        return w.fans.handle(req)
    pilot=build_supervised_pilot(w.store,env,model_transport=model,
        fans_transport=httpx.MockTransport(provider))
    holder["pilot"]=pilot
    stage(pilot,23420,"hello");assert pilot.tick()["dispatched"]==["unknown"]
    for _ in range(3):pilot.tick()
    assert len(posts)==1
    with Session(w.database[1]) as s:
        assert s.get(Message,(ACCOUNT,FAN,23420)).is_unsent
        assert s.scalar(select(OfTextIntent).where(OfTextIntent.status=="unknown")) is not None
