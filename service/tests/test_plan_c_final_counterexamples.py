"""Independent final-review counterexamples. Synthetic actors/providers, disposable PG only."""
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_engine_real_worker_pg import world, inbound, FAN2
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN
from test_plan_c_draft_app_pg import configured_draft, signed
from test_plan_c_text_activation_pg import stopped_text, start
from model_transport.pilot_supervised import build_supervised_pilot
from plan_c_worker import create_app
from db.onlyfans_engine_models import OfEngineModelCall

def model_reply(body, finish="stop"):
    return httpx.Response(200,json={
      'model':'m/primary',
      'choices':[{'message':{'content':json.dumps({'body':body})},'finish_reason':finish}],
      'usage':{'prompt_tokens':100,'completion_tokens':20,'cost':0.0001},
      'openrouter_metadata':{'endpoints':{'available':[{'provider':'Fixture Provider','selected':True}]}}})

def incoming_http(pilot, mid, body, fan=FAN):
    raw,headers=signed(inbound(mid,body,fan=fan),key='independent-review-'+str(mid))
    with TestClient(create_app(pilot,run_loop=False)) as client:
        result=client.post('/onlyfans/webhooks/plan-c',content=raw,headers=headers)
    assert result.status_code == 202

@pytest.mark.parametrize('body,allowed', [
  ('The photo costs four.',False),
  ('The photo costs 4.',False),
  ('It is a ten-minute video.',False),
  ('Good to hear from you.',True),
  ('That is not a video.',True),
])
def test_text_only_normal_path_does_not_deliver_unsupported_product_claims(stopped_text, body, allowed):
    w,env,_,seen,first=stopped_text
    start(w,first['revision'])
    def mock(request):
        seen.append(json.loads(request.content))
        return model_reply(body)
    pilot=build_supervised_pilot(w.store,env,model_transport=httpx.MockTransport(mock),fans_transport=w.fans.transport)
    incoming_http(pilot, 73101,'How much is the photo?')
    result=pilot.tick()
    print('TEXT_ONLY_RESULT',json.dumps({'body':body,'modelCalls':len(seen),'result':result,'posts':w.fans.posts},default=str))
    assert bool(w.fans.posts) is allowed
    if w.fans.posts:
        assert not w.fans.posts[0]['body'].get('price') and not w.fans.posts[0]['body'].get('mediaFiles')

@pytest.mark.parametrize('unsupported',['The photo costs four.','It is a ten-minute video.'])
def test_text_only_unsupported_product_wording_gets_one_correction_then_a_truthful_reply_is_sent(stopped_text, unsupported):
    """The unsupported text is never sent; the bounded correction path may deliver a different truthful answer."""
    w,env,_,seen,first=stopped_text
    start(w,first['revision'])
    def mock(request):
        seen.append(json.loads(request.content))
        return model_reply(unsupported if len(seen)==1 else 'Good to hear from you.')
    pilot=build_supervised_pilot(w.store,env,model_transport=httpx.MockTransport(mock),fans_transport=w.fans.transport)
    incoming_http(pilot, 73121,'How much is the photo?')
    pilot.tick()
    sent=[p['body']['text'] for p in w.fans.posts]
    print('CORRECTION_RESULT',json.dumps({'unsupported':unsupported,'modelCalls':len(seen),'sent':sent}))
    assert len(seen)==2 and sent==['Good to hear from you.']
    assert not w.fans.posts[0]['body'].get('price') and not w.fans.posts[0]['body'].get('mediaFiles')


def test_text_only_unsupported_wording_twice_is_never_sent_and_is_not_retried_again(stopped_text):
    w,env,_,seen,first=stopped_text
    start(w,first['revision'])
    def mock(request):
        seen.append(json.loads(request.content))
        return model_reply('The photo costs four.')
    pilot=build_supervised_pilot(w.store,env,model_transport=httpx.MockTransport(mock),fans_transport=w.fans.transport)
    incoming_http(pilot, 73131,'How much is the photo?')
    for _ in range(2):
        try: pilot.tick()
        except Exception as exc: print('TICK_RAISED',type(exc).__name__,str(exc)[:120])
    assert not w.fans.posts and len(seen)==2


@pytest.mark.parametrize('crash',[False,True])
def test_length_completion_restart_keeps_incomplete_output_ineligible(stopped_text, crash):
    w,env,_,seen,first=stopped_text
    start(w,first['revision'])
    def mock(request):
        seen.append(json.loads(request.content))
        return model_reply('Here is the incomplete reply.' if len(seen)==1 else 'Good to hear from you.',
                           'length' if len(seen)==1 else 'stop')
    transport=httpx.MockTransport(mock)
    pilot=build_supervised_pilot(w.store,env,model_transport=transport,fans_transport=w.fans.transport)
    incoming_http(pilot,73111,'hello there')
    if crash:
        runtime=pilot.worker.engine.model
        def interrupted(*a,**kw):
            raise RuntimeError('injected crash before completion verdict persistence')
        runtime.ledger.set_outcome=interrupted
        with pytest.raises(RuntimeError,match='injected crash'):
            pilot.tick()
        assert not w.fans.posts
        with Session(w.database[1]) as s:
            saved=list(s.scalars(select(OfEngineModelCall)))
            print('PERSISTED_AFTER_CRASH',[(r.status,r.outcome,r.response_text,r.actual_microusd) for r in saved])
            assert len(saved)==1 and saved[0].outcome == 'length_limited'  # was 'identity_verified' at baseline: that persisted state IS the defect (verdict lost); the length verdict is now durable
        pilot=build_supervised_pilot(w.store,env,model_transport=transport,fans_transport=w.fans.transport)
    result=pilot.tick()
    delivered=[p['body']['text'] for p in w.fans.posts]
    print('LENGTH_RESTART_RESULT',json.dumps({'crash':crash,'calls':len(seen),'delivered':delivered,'result':result},default=str))
    assert delivered == ['Good to hear from you.']
    assert len(seen)==2
