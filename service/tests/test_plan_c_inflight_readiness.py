"""Bounded phase progress protects legitimate primary/correction calls, never a hung loop."""
import threading,types
from unittest.mock import patch
from fastapi.testclient import TestClient
import pytest,httpx
import plan_c_worker as worker
from onlyfans_runtime.work_progress import started,observe
from conversation_engine.model_client import ChatRequest,ModelHTTPStatus
from model_transport.openrouter import OpenRouterClient

WORK={'accountHold':False,'eligibleFans':2,'heldFans':0,'unreviewedInbound':0,
      'unknownSends':0,'pendingProviderEvents':0,'malformedHolds':0}

def test_primary_correction_deadline_and_hard_failure_stay_distinct():
 clock=[5000.0];entered=threading.Event();correction=threading.Event();second=threading.Event();release=threading.Event()
 class Pilot:
  ticks=0;refuse=False
  def validate(self):
   if self.refuse:raise RuntimeError("synthetic authorization refusal")
  def tick(self):
   self.ticks+=1
   if self.ticks==2:
    started("model",45);entered.set()
    assert correction.wait(15)
    started("model",45);second.set()
    assert release.wait(15)
 pilot=Pilot()
 with patch.object(worker,'time',types.SimpleNamespace(monotonic=lambda:clock[0])),patch.object(worker,'_work_counts',lambda p:WORK):
  with TestClient(worker.create_app(pilot)) as client:
   try:
    assert entered.wait(6)
    clock[0]=5031
    response=client.get('/readyz');assert response.status_code==200,response.text
    assert response.json()['inFlight']['deadlineInSeconds']==19
    pilot.refuse=True
    assert client.get('/readyz').json()['code']=='scope_or_state_refused'
    pilot.refuse=False;clock[0]=5044;correction.set();assert second.wait(3)
    clock[0]=5075
    assert client.get('/readyz').status_code==200
    clock[0]=5095
    stale=client.get('/readyz');assert stale.status_code==503 and stale.json()['code']=='loop_stale'
    for _ in range(3):assert client.get('/readyz').status_code==503
    task=client.app.state.loop_task
    client.app.state.loop_task=types.SimpleNamespace(done=lambda:True)
    assert client.get('/readyz').json()['code']=='loop_dead'
    client.app.state.loop_task=task
   finally:release.set();correction.set()

def test_bounded_signals_come_from_actual_model_and_provider_clients():
 observations=[]
 client=OpenRouterClient(key_env='KEY',environ={'KEY':'fixture-only'},transport=httpx.MockTransport(lambda r:httpx.Response(429,json={})))
 with observe(lambda phase,seconds:observations.append((phase,seconds))):
  with pytest.raises(ModelHTTPStatus):client.chat(ChatRequest(model='fixture/model',messages=({'role':'user','content':'hi'},),timeout_seconds=45))
 assert observations==[('model',50)]
 from fans_api_adapter.real_transport import build_client,RealFansConfig
 def identity(request):
  from test_engine_real_worker_pg import answer
  return answer(200,[{'id':'acct_phase','onlyfans_id':'108','is_authenticated':True}])
 provider=build_client(RealFansConfig('fixture-only','acct_phase','https://api.fansapi.com',12),creator_id='108',epoch=1,transport=httpx.MockTransport(identity))
 with observe(lambda phase,seconds:observations.append((phase,seconds))):
  assert provider.identity().state=='verified'
 assert observations[-1]==('provider',17)
 # A phase report outside a worker context cannot renew any readiness state.
 started("model",45)
 assert len(observations)==2
