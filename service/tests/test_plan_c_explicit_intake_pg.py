"""Cold restart the actual isolated intake entrypoint with active durable scope."""
import importlib.util
from pathlib import Path
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from db.models import OfConversationState,OfCreatorBinding
from db.onlyfans_models import OfRuntime
from db.onlyfans_engine_models import OfFansWebhook
from model_transport.pilot_supervised import IntakeOnlyPilot
from conversation_engine import pilot_scope
from onlyfans_runtime.store import Refused,utcnow
from test_plan_c_text_no_offers_pg import text_no_offers
from test_plan_c_text_activation_pg import stopped_text
from test_plan_c_draft_app_pg import configured_draft,signed
from test_engine_real_worker_pg import world,inbound
from test_onlyfans_foundation_pg import database
from test_onlyfans_text_delivery_pg import ACCOUNT,FAN,TENANT
from test_plan_c_scope_pg import ACTOR,key

spec=importlib.util.spec_from_file_location('plan_c_explicit_receiver',Path(__file__).resolve().parents[2]/'ops/plan-c-supervision/receive_only.py')
receiver=importlib.util.module_from_spec(spec);spec.loader.exec_module(receiver)

def environment(monkeypatch,w,env):
 for k,v in env.items():monkeypatch.setenv(k,v)
 monkeypatch.setenv('DATABASE_URL',w.database[0]);monkeypatch.setenv('TWIN_PLAN_C_LISTEN_PORT','55462')
 # These deliberately unavailable credentials must never be opened or used.
 monkeypatch.setenv('ENGINE_MODEL_ENABLED','1');monkeypatch.setenv('ENGINE_MODEL_KEY_FILE','/missing/model.key')
 monkeypatch.setenv('TWIN_ENGINE_FANSAPI_ENV_FILE','/missing/provider.env')


def test_actual_entrypoint_active_no_offers_cold_restarts_accept_signed_and_preserve_stop(stopped_text,monkeypatch):
 w,env,_,seen=text_no_offers(stopped_text);environment(monkeypatch,w,env)
 unsigned,_=signed(inbound(34001,'hello'),key='intake-unsigned')
 stop,stop_headers=signed(inbound(34002,'<p>stop messaging me</p>'),key='explicit-intake-stop')
 greeting,headers=signed(inbound(34003,'hello again'),key='explicit-intake-greeting')
 runs=[]
 def serve(app,**kw):
  assert type(app.state.pilot) is IntakeOnlyPilot
  assert kw=={'host':'127.0.0.1','port':55462,'access_log':False}
  with TestClient(app) as client:
   assert client.post('/onlyfans/webhooks/plan-c',content=unsigned).status_code==401
   assert client.post('/onlyfans/webhooks/plan-c',content=stop,headers=stop_headers).status_code==202
   assert client.post('/onlyfans/webhooks/plan-c',content=greeting,headers=headers).status_code==202
   ready=client.get('/readyz').json();assert ready['mode']=='intake_only' and not ready['sendingReady']
  runs.append(True)
 with patch('model_transport.pilot_supervised.build_supervised_pilot',side_effect=AssertionError('active factory called')) as active,patch('model_transport.pilot_supervised.build_engine_service',side_effect=AssertionError('model constructed')) as model,patch('model_transport.pilot_supervised.build_real_transport',side_effect=AssertionError('send constructed')) as send,patch('uvicorn.run',side_effect=serve):
  receiver.main();receiver.main()
  active.assert_not_called();model.assert_not_called();send.assert_not_called()
 assert len(runs)==2 and not seen and not w.fans.posts
 with Session(w.database[1]) as s:
  assert s.get(OfFansWebhook,(ACCOUNT,1,'explicit-intake-stop')).raw_body==stop
  assert s.get(OfFansWebhook,(ACCOUNT,1,'explicit-intake-greeting')).raw_body==greeting
  assert s.get(OfConversationState,(ACCOUNT,FAN)).mode=='no_contact'


def test_receiver_stays_intake_after_durable_status_and_mode_changes(stopped_text,monkeypatch):
 w,env,_,seen=text_no_offers(stopped_text);environment(monkeypatch,w,env)
 v=pilot_scope.describe(w.store,TENANT,ACCOUNT)
 pilot_scope.set_status(w.store,TENANT,ACCOUNT,'stopped',expected_revision=v['revision'],command_key=key(34010),actor=ACTOR,reason='test receiver across scope change')
 v=pilot_scope.describe(w.store,TENANT,ACCOUNT);cfg=v['config'];cfg['delivery_mode']='full'
 pilot_scope.configure(w.store,TENANT,ACCOUNT,cfg,expected_revision=v['revision'],command_key=key(34011),actor=ACTOR,reason='full mode never changes receiver role')
 with patch('uvicorn.run') as run,patch('model_transport.pilot_supervised.build_supervised_pilot',side_effect=AssertionError('active factory called')):
  receiver.main();assert type(run.call_args.args[0].state.pilot) is IntakeOnlyPilot
 assert not seen and not w.fans.posts


@pytest.mark.parametrize('problem',['account','tenant','binding'])
def test_receiver_rejects_wrong_account_tenant_or_revoked_binding(stopped_text,monkeypatch,problem):
 w,env,_,_=text_no_offers(stopped_text);environment(monkeypatch,w,env)
 if problem=='account':monkeypatch.setenv('TWIN_PLAN_C_ACCOUNT','another-account')
 elif problem=='tenant':monkeypatch.setenv('TWIN_PLAN_C_TENANT','another-tenant')
 else:
  with Session(w.database[1]) as s,s.begin():
   runtime=s.get(OfRuntime,ACCOUNT);s.get(OfCreatorBinding,runtime.binding_id).revoked_at=utcnow()
 with patch('uvicorn.run') as run,pytest.raises(Refused):receiver.main()
 run.assert_not_called()


@pytest.mark.parametrize('change',[{'ONLYFANS_PROCESS_ROLE':'api'},{'TWIN_PLAN_C_SUPERVISED':'0'},{'TWIN_PLAN_C_LISTEN_PORT':'80'},{'TWIN_PLAN_C_LISTEN_PORT':'65536'},{'TWIN_PLAN_C_LISTEN_PORT':'127.0.0.1'},{'TWIN_PLAN_C_LISTEN_PORT':'55462 '}])
def test_receiver_rejects_invalid_role_flag_or_port_before_store(monkeypatch,change):
 for k,v in {'ONLYFANS_PROCESS_ROLE':'worker','TWIN_PLAN_C_SUPERVISED':'1','DATABASE_URL':'unused','TWIN_PLAN_C_TENANT':'pilot','TWIN_PLAN_C_ACCOUNT':'account',**change}.items():monkeypatch.setenv(k,v)
 with patch.object(receiver,'Store') as store,pytest.raises(Refused):receiver.main()
 store.assert_not_called()
