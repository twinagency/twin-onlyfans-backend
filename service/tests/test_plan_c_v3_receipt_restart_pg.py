
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from db.models import OfConversationState, Message
from db.onlyfans_delivery_models import OfTextIntent
from db.onlyfans_engine_models import OfEngineDecision, OfEngineRequest
from test_plan_c_supervised_pg import supervised, stage, approve
from test_plan_c_draft_app_pg import configured_draft
from test_engine_real_worker_pg import world
from test_onlyfans_foundation_pg import database
from test_onlyfans_text_delivery_pg import TENANT, ACCOUNT, FAN
from model_transport.pilot_supervised import build_supervised_pilot

def snapshot(world):
    with Session(world.database[1]) as s:
        intent=s.scalar(select(OfTextIntent))
        decision=s.scalar(select(OfEngineDecision))
        request=s.scalar(select(OfEngineRequest))
        state=s.get(OfConversationState,(ACCOUNT,FAN))
        return {"intent":intent.status,"projected":str(intent.projected_at),
                "decision":decision.status,"request":request.status,"next_step":request.next_step,
                "unresolved_attempt":bool(state.unresolved_attempt_id),
                "message":s.get(Message,(ACCOUNT,FAN,intent.provider_message_id)) is not None}

def test_restart_materializes_accepted_receipt(supervised,monkeypatch):
    world,env,model,seen,pilot=supervised
    result=stage(pilot,99102)
    approve(world,result)
    authority=pilot.worker.authority
    original=authority.materialize
    def crash(*args,**kwargs):
        raise RuntimeError("audit crash after accepted receipt committed")
    monkeypatch.setattr(authority,"materialize",crash)
    with pytest.raises(RuntimeError,match="audit crash"):
        pilot.worker.send_approved(TENANT,ACCOUNT,result["decisionId"])
    assert len(world.fans.posts)==1
    print("AUDIT_BEFORE_RESTART",snapshot(world))
    monkeypatch.setattr(authority,"materialize",original)
    restarted=build_supervised_pilot(world.store,env,model_transport=model,fans_transport=world.fans.transport)
    ticks=[restarted.tick() for _ in range(3)]
    final=snapshot(world)
    print("AUDIT_AFTER_RESTART",{"state":final,"ticks":ticks,"posts":len(world.fans.posts)})
    assert len(world.fans.posts)==1, "accepted send was duplicated"
    assert final["projected"]!="None", "Restart left accepted receipt unmaterialized"
    assert final["decision"]=="completed" and not final["unresolved_attempt"]
