"""Ten concurrent autonomous workers: one stalled model and one spent account do not freeze peers."""
import json,threading
from concurrent.futures import ThreadPoolExecutor
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session
from commercial_worker_fixture import build_world,FAN_A,OPERATOR
from conversation_engine.writer_context import set_active_profiles
from db.onlyfans_delivery_models import OfDeliveryPolicy,OfTextIntent
from onlyfans_runtime.text_delivery import TextAuthority
from test_onlyfans_foundation_pg import database
from test_commercial_preparation_pg import TENANT

def test_ten_concurrent_workers_keep_stalled_and_exhausted_accounts_local(database,tmp_path,monkeypatch):
 fleet=[];entered=threading.Event();release=threading.Event()
 def stalled(request):
  entered.set()
  assert release.wait(180),"test-controlled model was never released"
  return httpx.Response(429,headers={"Retry-After":"30"},json={"error":"fixture rate limit"})
 try:
  for i in range(1,11):
   folder=tmp_path/str(i);folder.mkdir()
   w=build_world(database,tmp_path,monkeypatch,i=i,fans=(FAN_A,),folder=folder)
   w.product("Beach photo",[31],standard=1000+100*i,description="One beach photograph.")
   if i==10:w.model_transport=httpx.MockTransport(stalled)
   w.launch(authorization_mode="autonomous");fleet.append(w)
  team=tmp_path/"team.json"
  team.write_text(json.dumps({"version":"provider-team.v1","team":"concurrent-workers","accounts":[w.account for w in fleet],
                             "creditLimit":10000,"externalReserved":0}))
  monkeypatch.setenv("TWIN_PROVIDER_TEAM_LIMITS_FILE",str(team))
  spent=fleet[8]
  with Session(database[1]) as s:revision=s.get(OfDeliveryPolicy,spent.account).revision
  TextAuthority(spent.store).configure(TENANT,spent.account,expected_revision=revision,enabled=True,media_enabled=True,
      limit_units=1,daily_limit_units=1,throttle_seconds=1,transport_kind="fansapi",actor=OPERATOR,reason="One-unit reviewed fixture budget")
  spent.converse("hello",FAN_A)
  assert len(spent.fans_mock.posts)==1
  with ThreadPoolExecutor(max_workers=10) as pool:
   jobs=[pool.submit(w.converse,"show me the beach photo",FAN_A) for w in fleet]
   try:
    assert entered.wait(90),"stalled account never reached its model"
    for job in jobs[:9]:job.result(timeout=120)
    assert not jobs[9].done(),"fixture must still have one in-flight model call"
    for i,w in enumerate(fleet[:8],1):
     assert len(w.cards())==1
     assert w.cards()[0]["body"]["mediaFiles"]==["31"]
     assert w.cards()[0]["body"]["price"]==(1000+100*i)/100
    assert not spent.cards() and not fleet[9].fans_mock.posts
    with Session(database[1]) as s:
     for w in fleet[:8]:
      assert len(s.scalars(select(OfTextIntent).where(OfTextIntent.account_id==w.account,OfTextIntent.status=="accepted")).all())==1
   finally:release.set()
   jobs[9].result(timeout=60)
  assert not fleet[9].fans_mock.posts
 finally:
  release.set();set_active_profiles(None)
  for w in fleet:w.store.close()
