"""Memory correction during regeneration, old authorization, restart and recovery."""
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_engine_real_worker_pg import world
from test_plan_c_draft_app_pg import configured_draft
from test_plan_c_supervised_pg import supervised,stage
from test_offline_completion_pg import normal_pilot
from test_plan_c_grant_lifecycle_pg import correct_memory
from test_plan_c_scope_pg import ACTOR
from test_onlyfans_text_delivery_pg import ACCOUNT,TENANT,FAN
from onlyfans_runtime.launch_history_memory.memory import HistoricalMemory
from onlyfans_runtime.store import Store,Refused
from conversation_engine import approval
from db.onlyfans_engine_models import OfEngineDecision,OfEngineRequest
from model_transport.pilot_supervised import build_supervised_pilot

@pytest.mark.parametrize("supervised",["autonomous"],indirect=True)
def test_memory_correction_in_flight_fences_old_authorization_and_recovers_without_input(supervised):
 w,env,_,_,_=supervised
 phase={"armed":False,"injected":False}
 def during():
  if phase["armed"] and not phase["injected"]:
   phase["injected"]=True
   memory=HistoricalMemory(w.store)
   memory.correct(TENANT,ACCOUNT,FAN,field="home_city",operation="set",value="Perth",
    expected_version=memory.read(TENANT,ACCOUNT,FAN)["version"],command_key="combined-memory-in-flight",
    actor=ACTOR,reason="offline correction while replacement generation is in flight")
 pilot,seen,_,model=normal_pilot(w,env,selecting=False,on_call=during)
 staged=stage(pilot,18620,"hello")
 info=staged["approval"]
 correct_memory(w,"combined-memory-before-regeneration")
 phase["armed"]=True
 for _ in range(4):
  pilot.tick()
  if phase["injected"]:break
 assert phase["injected"] and w.fans.posts==[]
 with pytest.raises(Refused):
  approval.authorize_automatic(w.store,TENANT,ACCOUNT,info["requestId"],info["generation"],expected_version=info["version"])
 fresh_store=Store(w.database[0])
 try:
  fresh=build_supervised_pilot(fresh_store,env,model_transport=model,fans_transport=w.fans.transport)
  for _ in range(6):fresh.tick()
  assert len(w.fans.posts)==1 and len(seen)>=3
  assert "Perth" in seen[-1]["messages"][-1]["content"]
  with Session(w.database[1]) as s:
   request=s.get(OfEngineRequest,(ACCOUNT,info["requestId"]))
   assert request.status=="completed"
   latest=s.scalar(select(OfEngineDecision).where(OfEngineDecision.account_id==ACCOUNT,OfEngineDecision.request_id==info["requestId"]).order_by(OfEngineDecision.generation.desc()))
   assert latest.generation>info["generation"] and latest.status=="completed"
 finally:fresh_store.close()
