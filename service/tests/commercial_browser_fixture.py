"""Offline browser/BFF/PG fixture. Synthetic identity/model/provider; real authority."""
import hashlib,json,os,tempfile,time
from pathlib import Path
from datetime import timedelta
from contextlib import contextmanager
from fastapi import FastAPI
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from db.models import Account,Fan,OfCreatorBinding,OfConversationState
from db.onlyfans_engine_models import OfEngineOfferGrant,OfEngineAccessEvidence,OfEngineModelCall
from db.onlyfans_delivery_models import OfTextIntent
from onlyfans_runtime.store import Store,Event,utcnow
from onlyfans_runtime.text_delivery import TextAuthority
from onlyfans_runtime.worker import ChatPage,MessagePage,read_step
from onlyfans_read.agency_scope import AgencyScopeMiddleware
from onlyfans_read.api import router as reads
from onlyfans_commands.configuration_api import router as config_router
from onlyfans_commands.vault_api import router as vault
from onlyfans_commands.content_api_v2 import router as content
from onlyfans_commands.engine_approval_api import router as operator
from onlyfans_commands.api import router as controls
from conversation_engine import preparation,pilot_scope,commercial_lifecycle
from conversation_engine.writer_context import CreatorProfile,profile_data
from conversation_engine.model_config import ModelConfig
from model_transport.pilot_supervised import build_supervised_pilot
from commercial_worker_fixture import World,CommercialFans,writer,SECRET,FAN_A
from test_commercial_preparation_pg import config,TENANT
from test_onlyfans_foundation_pg import database
from test_real_transport_pg import _item
from history_support import Provider,factory,page,chat_page,fan_message

ACCOUNT='browser-normal'
CREATOR='11111111-1111-4111-8111-111111111111'
OWNER='44444444-4444-4444-8444-444444444444'
ACTOR='op-session:'+hashlib.sha256(('twin-plan-c-named-owner-v1:'+OWNER).encode()).hexdigest()
db=database.__wrapped__();owned=next(db);store=Store(owned[0])
with Session(owned[1]) as s,s.begin():
 s.add(Account(id=ACCOUNT,nickname='Offline browser fixture'));s.flush()
 s.add(OfCreatorBinding(id='browser-binding',tenant_id=TENANT,creator_id=CREATOR,creator_name='Offline browser fixture',account_id=ACCOUNT,handle='fixture',channel='onlyfans'))
 s.add(Fan(account_id=ACCOUNT,fan_id=FAN_A));s.flush()
 s.add(OfConversationState(account_id=ACCOUNT,fan_id=FAN_A,mode='paused',revision=0,takeover_state='none',requires_attention=False))
store.register(TENANT,ACCOUNT,activated_at=utcnow()-timedelta(hours=1),enabled=False)
TextAuthority(store).configure(TENANT,ACCOUNT,expected_revision=0,enabled=False,media_enabled=False,limit_units=0,throttle_seconds=1,transport_kind='fansapi',actor=ACTOR,reason='Disabled offline browser connector')
cfg=config(8,requests=1000,credits=2000)
provider=Provider(cfg);provider.vault=[lambda:page([_item(31)])];provider.chats[FAN_A]=[chat_page([fan_message(701,FAN_A,'Earlier hello')])]
profile=profile_data(CreatorProfile(name='Offline fixture',facts=(('home_city','Brisbane'),),limits=('no meetups',),role_statement='the creator herself',offers_permitted=True,approved_item_ids=()))
tmp=tempfile.TemporaryDirectory();root=Path(tmp.name)
(root/'secret').write_bytes(SECRET);(root/'secret').chmod(0o600)
(root/'fans').write_text('FANSAPI_KEY=fixture-key\nFANSAPI_ACCT='+cfg['provider_account']+'\n');(root/'fans').chmod(0o600)
(root/'profiles').write_text(json.dumps({ACCOUNT:profile}))
env={'TWIN_PLAN_C_SUPERVISED':'1','TWIN_PLAN_C_DRAFT_ONLY':'0','TWIN_PLAN_C_TENANT':TENANT,'TWIN_PLAN_C_ACCOUNT':ACCOUNT,
 'TWIN_PLAN_C_WEBHOOK_SECRET_FILE':str(root/'secret'),'ONLYFANS_PROCESS_ROLE':'worker','TWIN_ENGINE_FANSAPI_REAL':'1',
 'TWIN_ENGINE_FANSAPI_ENV_FILE':str(root/'fans'),'ENGINE_MODEL_ENABLED':'1','ENGINE_MODEL_PRIMARY':'m/primary',
 'ENGINE_MODEL_UPSTREAM':'fixture-provider','ENGINE_MODEL_UPSTREAM_REPORTED_NAME':'Fixture Provider','ENGINE_MODEL_ALLOWED':'m/primary',
 'ENGINE_MODEL_ACCOUNTS':ACCOUNT,'ENGINE_MODEL_KEY_ENV':'FIXTURE_KEY','FIXTURE_KEY':'fixture-only',
 'ENGINE_MODEL_PROMPT_MICROUSD_PER_MTOK':'1000000','ENGINE_MODEL_COMPLETION_MICROUSD_PER_MTOK':'2000000',
 'ENGINE_MODEL_DAILY_CAP_MICROUSD':'1000000','ENGINE_MODEL_CREATOR_CAP_MICROUSD':'1000000','ENGINE_MODEL_PROFILES_FILE':str(root/'profiles')}
os.environ.update(ONLYFANS_PROCESS_ROLE='api',TWIN_PLAN_C_FIXTURE_ONLY='1',TWIN_ENGINE_FANSAPI_REAL='1',
 ONLYFANS_API_TOKEN='synthetic-read',ONLYFANS_API_TENANT=TENANT,ONLYFANS_CONTENT_TOKEN='synthetic-content',ONLYFANS_CONTENT_TENANT=TENANT,
 ONLYFANS_CONFIGURATION_TOKEN='synthetic-configuration',ONLYFANS_CONFIGURATION_TENANT=TENANT,
 ONLYFANS_CONTROL_TOKEN='synthetic-control',ONLYFANS_CONTROL_TENANT=TENANT)
app=FastAPI();app.state.control_store=store;app.add_middleware(AgencyScopeMiddleware)
for router in (reads,config_router,vault,content,operator,controls):app.include_router(router)
world=World();world.store=store;world.database=owned;world.account=ACCOUNT;world.cfg=cfg;world.env=env
world.provider=cfg['provider_account'];world.creator=cfg['creator_id'];world.fans=(FAN_A,)
world.fans_mock=CommercialFans(world.provider,world.creator);world.model_transport=writer(world)
@contextmanager
def worker_role():
 old=os.environ['ONLYFANS_PROCESS_ROLE'];os.environ['ONLYFANS_PROCESS_ROLE']='worker'
 try:yield
 finally:os.environ['ONLYFANS_PROCESS_ROLE']=old

@app.get('/fixture')
def info():
 return {'account':ACCOUNT,'creator':CREATOR,'fan':FAN_A,'actor':ACTOR,'preparation':cfg,'profile':profile}
@app.post('/fixture/prepare')
def prepare():
 with worker_role():
  from fans_api_adapter.history_import import enlist_fan
  enlist_fan(store,ACCOUNT,FAN_A)
  for _ in range(30):preparation.prepare_one(store,TENANT,ACCOUNT,client_factory=factory(provider))
 return preparation.view(store,TENANT,ACCOUNT)
@app.post('/fixture/sync')
def sync():
 with worker_role():
  rev=pilot_scope.describe(store,TENANT,ACCOUNT)['revision']
  client=factory(provider)(cfg,1,preparation.PreparationRequestGuard(store,TENANT,ACCOUNT,rev))
  assert client.identity().state=='verified'
  class Reader:
   account_id=ACCOUNT
   def chats(self,**kw):return ChatPage([FAN_A],None)
   def messages(self,*,fan_id,before_id,limit):
    result=client.history(str(fan_id),limit=limit)
    assert result.state=='observed'
    return MessagePage([Event(fan_id,int(m['id']),m['createdAt'],'in',m['text']) for m in result.data],False)
  store.begin_scan(TENANT,ACCOUNT)
  for _ in range(4):read_step(store,TENANT,ACCOUNT,Reader())
 return {'ok':True}
@app.get('/fixture/promotion')
def promotion():
 snapshot=commercial_lifecycle.catalogue_snapshot(store,TENANT,ACCOUNT,evidence_ref='vault:browser-reviewed',valid_until=utcnow()+timedelta(days=1))
 return {'fans':[FAN_A],'creator_profile':profile,'delivery_mode':'full','catalogue':snapshot,
  'operator':{'actor':ACTOR,'name':'Offline named operator','permissions':['configure','activate','approve','reject','edit','stop','takeover','handback','reconcile','correct','read']},
  'model':pilot_scope.model_spec(ModelConfig.from_env(env)),
  'limits':{'send_units':200,'daily_send_units':100,'throttle_seconds':1,'provider_requests':4000,'provider_credit_budget':8000,'provider_credits_per_request':2}}
@app.post('/fixture/fan-message')
def fan_message_event(body:dict):
 with worker_role():
  if not hasattr(world,'pilot'):world.pilot=build_supervised_pilot(store,env,model_transport=world.model_transport,fans_transport=world.fans_mock.transport)
  world.say(body['text'],FAN_A)
  time.sleep(1.05);world.pilot.tick()
 return state()
@app.post('/fixture/unlock')
def unlock():
 with worker_role():
  with Session(owned[1]) as s:
   intent=s.scalar(select(OfTextIntent).where(OfTextIntent.account_id==ACCOUNT,OfTextIntent.price_cents>0,OfTextIntent.status=='accepted'))
   assert intent is not None
   mid=intent.provider_message_id
  world.deliver(world.unlock_event(mid,12));time.sleep(1.05);world.pilot.tick()
 return state()
@app.get('/fixture/state')
def state():
 with Session(owned[1]) as s:
  return {'posts':[p['body'] for p in world.fans_mock.posts],
   'grants':s.scalar(select(func.count()).select_from(OfEngineOfferGrant)),
   'access':s.scalar(select(func.count()).select_from(OfEngineAccessEvidence).where(OfEngineAccessEvidence.applied.is_(True))),
   'modelCalls':s.scalar(select(func.count()).select_from(OfEngineModelCall)),
   'scope':pilot_scope.describe(store,TENANT,ACCOUNT)}
if __name__=='__main__':
 import uvicorn
 try:uvicorn.run(app,host='0.0.0.0',port=8787,log_level='warning')
 finally:store.close();db.close();tmp.cleanup()
