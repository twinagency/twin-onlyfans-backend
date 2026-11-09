"""Actual text-only startup with the deployed no-commercial-permission profile."""
import json
from pathlib import Path
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from conversation_engine import pilot_scope
from conversation_engine.writer_context import JsonFileCreatorProfiles,profile_data
from db.onlyfans_delivery_models import OfDeliveryPolicy
from model_transport.pilot_supervised import build_supervised_pilot
from onlyfans_runtime.store import Refused
from plan_c_worker import create_app
from test_plan_c_text_activation_pg import stopped_text,start
from test_plan_c_supervised_pg import supervised
from test_plan_c_draft_app_pg import configured_draft,signed
from test_engine_real_worker_pg import world,inbound
from test_onlyfans_foundation_pg import database
from test_onlyfans_text_delivery_pg import ACCOUNT,FAN,TENANT
from test_plan_c_scope_pg import ACTOR,activate,key

def no_offers(w,env,view,*,mode='text_only',n=33001):
 path=Path(env['ENGINE_MODEL_PROFILES_FILE']);raw=json.loads(path.read_text())
 raw[ACCOUNT]['offers_permitted']=False;path.write_text(json.dumps(raw))
 cfg=json.loads(json.dumps(view['config']))
 cfg['creator_profile']=profile_data(JsonFileCreatorProfiles(str(path)).get(ACCOUNT))
 if mode is None:cfg.pop('delivery_mode',None)
 else:cfg['delivery_mode']=mode
 return pilot_scope.configure(w.store,TENANT,ACCOUNT,cfg,expected_revision=view['revision'],command_key=key(n),actor=ACTOR,reason='same approved profile with commercial permission disabled')

def text_no_offers(stopped_text):
 w,env,model,seen,_=stopped_text
 view=pilot_scope.describe(w.store,TENANT,ACCOUNT)
 changed=no_offers(w,env,view)
 start(w,changed['revision'],command=33002)
 return w,env,model,seen

def test_deployed_no_offers_profile_completes_stopped_text_startup_and_one_reply(stopped_text):
 w,env,model,seen=text_no_offers(stopped_text)
 pilot=build_supervised_pilot(w.store,env,model_transport=model,fans_transport=w.fans.transport)
 assert pilot.profiles.get(ACCOUNT).offers_permitted is False
 pilot.validate()
 raw,headers=signed(inbound(33003,'hello there'),key='text-no-offers-startup')
 with TestClient(create_app(pilot,run_loop=False)) as client:assert client.post('/onlyfans/webhooks/plan-c',content=raw,headers=headers).status_code==202
 assert not seen and not w.fans.posts
 assert pilot.tick()['dispatched']==['accepted']
 assert len(w.fans.posts)==1
 body=w.fans.posts[0]['body'];assert not body.get('price') and not body.get('mediaFiles')
 assert pilot.tick()['dispatched']==[] and len(w.fans.posts)==1

@pytest.mark.parametrize('mode',['full',None])
def test_full_and_legacy_no_offers_permission_still_refuse(supervised,mode):
 w,env,model,seen,_=supervised
 view=pilot_scope.describe(w.store,TENANT,ACCOUNT)
 pilot_scope.set_status(w.store,TENANT,ACCOUNT,'stopped',expected_revision=view['revision'],command_key=key(33010),actor=ACTOR,reason='stopped full-profile boundary')
 with Session(w.database[1]) as s:
  p=s.get(OfDeliveryPolicy,ACCOUNT);rev,limit,throttle=p.revision,p.limit_units,p.throttle_seconds
 w.authority.configure(TENANT,ACCOUNT,expected_revision=rev,enabled=False,limit_units=limit,throttle_seconds=throttle,actor=ACTOR,reason='stopped full-profile boundary')
 view=pilot_scope.describe(w.store,TENANT,ACCOUNT);changed=no_offers(w,env,view,mode=mode,n=33011)
 activate(w,revision=changed['revision'],n=33012)
 cfg=pilot_scope.describe(w.store,TENANT,ACCOUNT)['config']
 pilot_scope.require_profile(cfg,JsonFileCreatorProfiles(env['ENGINE_MODEL_PROFILES_FILE']).get(ACCOUNT))
 with pytest.raises(Refused,match='creator or journey configuration is incomplete'):
  build_supervised_pilot(w.store,env,model_transport=model,fans_transport=w.fans.transport)
 assert not seen and not w.fans.posts

def test_text_profile_change_refuses_after_successful_start(stopped_text):
 w,env,model,seen=text_no_offers(stopped_text)
 pilot=build_supervised_pilot(w.store,env,model_transport=model,fans_transport=w.fans.transport)
 path=Path(env['ENGINE_MODEL_PROFILES_FILE']);raw=json.loads(path.read_text());raw[ACCOUNT]['offers_permitted']=True;path.write_text(json.dumps(raw))
 with pytest.raises(Refused):pilot.validate()
 with pytest.raises(Refused):pilot.tick()
 assert not seen and not w.fans.posts

def test_text_mode_change_fences_the_started_worker(stopped_text):
 w,env,model,seen=text_no_offers(stopped_text)
 pilot=build_supervised_pilot(w.store,env,model_transport=model,fans_transport=w.fans.transport)
 view=pilot_scope.describe(w.store,TENANT,ACCOUNT)
 pilot_scope.set_status(w.store,TENANT,ACCOUNT,'stopped',expected_revision=view['revision'],command_key=key(33020),actor=ACTOR,reason='explicit mode change while stopped')
 view=pilot_scope.describe(w.store,TENANT,ACCOUNT);no_offers(w,env,view,mode='full',n=33021)
 with pytest.raises(Refused):pilot.validate()
 with pytest.raises(Refused):pilot.tick()
 assert not seen and not w.fans.posts

def test_forced_offer_proposal_cannot_reach_provider_in_no_offers_text_mode(stopped_text):
 w,env,_,seen=text_no_offers(stopped_text)
 def force_offer(request):
  seen.append(json.loads(request.content))
  return httpx.Response(200,json={'model':'m/primary','choices':[{'message':{'content':json.dumps({'body':'Here is the photo','action':'offer','offer_grant_id':'00000000-0000-0000-0000-000000000001'})},'finish_reason':'stop'}],'usage':{'prompt_tokens':100,'completion_tokens':20,'cost':0.0001},'openrouter_metadata':{'endpoints':{'available':[{'provider':'Fixture Provider','selected':True}]}}})
 pilot=build_supervised_pilot(w.store,env,model_transport=httpx.MockTransport(force_offer),fans_transport=w.fans.transport)
 raw,headers=signed(inbound(33030,'hello there'),key='text-force-offer')
 with TestClient(create_app(pilot,run_loop=False)) as client:assert client.post('/onlyfans/webhooks/plan-c',content=raw,headers=headers).status_code==202
 pilot.tick();assert seen and not w.fans.posts
 with Session(w.database[1]) as s:assert not s.get(OfDeliveryPolicy,ACCOUNT).media_enabled


def test_photo_request_cannot_send_a_card_with_commercial_permission_disabled(stopped_text):
 w,env,model,_=text_no_offers(stopped_text)
 pilot=build_supervised_pilot(w.store,env,model_transport=model,fans_transport=w.fans.transport)
 raw,headers=signed(inbound(33040,'send me the photo'),key='text-no-offers-photo-request')
 with TestClient(create_app(pilot,run_loop=False)) as client:assert client.post('/onlyfans/webhooks/plan-c',content=raw,headers=headers).status_code==202
 pilot.tick();assert not w.fans.posts
