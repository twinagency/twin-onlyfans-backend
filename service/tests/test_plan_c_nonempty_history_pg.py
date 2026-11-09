"""J01/J03/J07/J10: nonempty paginated history through existing guarded backfill and normal worker."""
from datetime import datetime,timedelta,timezone
import json,re
import httpx,pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_engine_real_worker_pg import world,inbound,unlock,answer,FAN2,PROVIDER,CREATOR
from test_plan_c_draft_app_pg import configured_draft,signed
from test_plan_c_supervised_pg import supervised
from test_plan_c_sales_journey_pg import install_coverage,cards
from test_onlyfans_text_delivery_pg import TENANT,ACCOUNT,FAN
from model_transport.pilot_supervised import build_supervised_pilot
from fans_api_adapter.backfill import FansBackfill
from conversation_engine import pilot_scope
from db.models import Message,Transaction
from db.onlyfans_engine_models import OfEngineOfferGrant,OfEngineApproval,OfEngineRequest
from plan_c_worker import create_app

def send_http(pilot,mid,body,fan=FAN):
 raw,headers=signed(inbound(mid,body,fan=fan),key='joined-'+str(mid))
 with TestClient(create_app(pilot,run_loop=False)) as client:
  assert client.post('/onlyfans/webhooks/plan-c',content=raw,headers=headers).status_code==202

def setup_history(w,env,*,recent=False,purchased=False):
 from db.models import CatalogItem
 with Session(w.database[1]) as s,s.begin():
  item=s.get(CatalogItem,w.item);item.label='Garden photo';item.description_for_ai='One scenic garden photograph.'
 install_coverage(w)
 when=(datetime.now(timezone.utc)-timedelta(hours=0 if recent else 2)).isoformat()
 pages,seen=[],[]
 def incoming(mid,fan,body):
  return {'id':mid,'fromUser':{'id':fan},'text':body,'createdAt':when,'isSentByMe':False,'media':[],'price':0}
 def provider(request):
  if request.method=='GET':
   if '/transactions' in request.url.path:return answer(200,{'data':{'list':[],'hasMore':False}})
   if '/chargebacks' in request.url.path:return answer(200,{'data':{'list':[]}})
   if request.url.path.endswith('/messages'):
    fan=int(request.url.path.split('/chats/')[1].split('/')[0]);older='id' in request.url.params
    pages.append((fan,older))
    first=31000 if fan==FAN else 32000
    note='Riverside garden context' if fan==FAN else 'Astronomy context'
    overlap=incoming(first+1,fan,note)
    entries=[overlap,incoming(first,fan,'Older ordinary message')] if older else [incoming(first+2,fan,'Recent history note'),overlap]
    if fan==FAN and not older:
     entries.append({'id':30999,'fromUser':{'id':int(CREATOR)},'isSentByMe':True,
      'text':'Earlier photo from provider history','createdAt':when,'price':3 if purchased else 0,
      'media':[{'id':11 if purchased else 13,'type':'photo'}],'isOpened':purchased})
    next_page=None if older else f'https://app.onlyfansapi.com/api/{PROVIDER}/chats/{fan}/messages?limit=3&order=desc&id={first+1}'
    return answer(200,{'data':entries,'_pagination':{'next_page':next_page}})
  return w.fans.handle(request)
 def model(request):
  data=json.loads(request.content);seen.append(data);prompt=data['messages'][-1]['content']
  latest=prompt.split('LATEST:\nFAN: ',1)[1].split('\n\nTHIS TURN:',1)[0]
  approved=re.search(r'APPROVED OFFER: id ([0-9a-f-]{36}),',prompt)
  if latest=='I would like the photo' and approved:
   proposal={'body':'Here is the photo.','action':'offer','offer_grant_id':approved[1],'pitch':True}
  elif latest=='Can I open the photo?':
   proposal={'body':'You have access to the Garden photo.'}
  elif latest=='Can we talk about astronomy?':
   proposal={'body':'Yes, we can talk about astronomy.'}
  else:proposal={'body':'Thanks for your message.'}
  return httpx.Response(200,json={'model':'m/primary','choices':[{'message':{'content':json.dumps(proposal)},'finish_reason':'stop'}],
   'usage':{'prompt_tokens':100,'completion_tokens':20,'cost':0.0001},
   'openrouter_metadata':{'endpoints':{'available':[{'provider':'Fixture Provider','selected':True}]}}})
 pilot=build_supervised_pilot(w.store,env,model_transport=httpx.MockTransport(model),fans_transport=httpx.MockTransport(provider))
 with Session(w.database[1]) as s:
  assert not list(s.scalars(select(OfEngineOfferGrant)))
  assert not list(s.scalars(select(OfEngineApproval)))
  assert not list(s.scalars(select(Transaction)))
 raw,headers=signed({'event':'accounts.connected','account_id':PROVIDER,'payload':{'onlyfans_id':CREATOR}},key='joined-account-connected')
 with TestClient(create_app(pilot,run_loop=False)) as client:
  assert client.post('/onlyfans/webhooks/plan-c',content=raw,headers=headers).status_code==202
 pilot.worker.intake.reproject_pending(ACCOUNT)
 backfill=FansBackfill(pilot.worker.intake)
 first=backfill.run(TENANT,ACCOUNT,pilot.worker.transport.client,reason='operator',
  start_date=datetime(2015,1,1),pages_budget=3,history_chats=(FAN,FAN2))
 assert first['status']=='budget_exhausted' and first['rows']>0
 assert not seen and not w.fans.posts
 final=backfill.run(TENANT,ACCOUNT,pilot.worker.transport.client,reason='operator',pages_budget=4,history_chats=(FAN,FAN2))
 assert final['status']=='complete' and final['rows']>=8 and len(pages)==4
 assert not seen and not w.fans.posts
 return pilot,seen,pages,final

def polls(w,pilot,n=3):
 for _ in range(n):w.no_throttle();pilot.tick()

@pytest.mark.parametrize('supervised',['autonomous'],indirect=True)
def test_nonempty_overlap_history_sale_unlock_and_fan_isolation(supervised):
 w,env,_,_,_=supervised
 pilot,seen,pages,history=setup_history(w,env)
 send_http(pilot,24300,'I would like the photo')
 send_http(pilot,24301,'Can we talk about astronomy?',fan=FAN2)
 polls(w,pilot)
 assert len(cards(w))==1 and len(w.fans.posts)==2
 card=cards(w)[0];assert card['body']['price']==3 and card['body']['mediaFiles']==['11']
 fan1prompt=next(d['messages'][-1]['content'] for d in seen if 'LATEST:\nFAN: I would like the photo' in d['messages'][-1]['content'])
 fan2prompt=next(d['messages'][-1]['content'] for d in seen if 'LATEST:\nFAN: Can we talk about astronomy?' in d['messages'][-1]['content'])
 assert 'Astronomy context' not in fan1prompt and 'Riverside garden context' not in fan2prompt
 message_id=w.fans.stored[card['key']]['data']['id']
 raw,headers=signed(unlock(message_id,amount=3),key='joined-exact-unlock')
 with TestClient(create_app(pilot,run_loop=False)) as client:
  assert client.post('/onlyfans/webhooks/plan-c',content=raw,headers=headers).status_code==202
 send_http(pilot,24302,'Can I open the photo?');polls(w,pilot)
 assert len(cards(w))==1 and len(w.fans.posts)==3
 assert 'You have access to the Garden photo.' in w.fans.posts[-1]['body']['text']
 with Session(w.database[1]) as s:
  assert s.scalar(select(OfEngineRequest).where(OfEngineRequest.source_message_id==24302)).status=='completed'
  historic=s.get(Message,(ACCOUNT,FAN,30999))
  assert historic is not None,'guarded backfill must retain historical creator media context'
  assert historic.price_cents==0 and historic.media_count==1
 assert 'free media' in fan1prompt and 'Earlier photo from provider history' in fan1prompt

@pytest.mark.parametrize('supervised',['autonomous'],indirect=True)
def test_pre_activation_history_does_not_initiate_new_unsolicited_replies(supervised):
 w,env,_,_,_=supervised
 pilot,seen,_,_=setup_history(w,env)
 polls(w,pilot)
 assert not seen and not w.fans.posts,'old historical inputs are context, not fresh reply authority'

@pytest.mark.parametrize('supervised',['autonomous'],indirect=True)
def test_post_activation_missed_input_recovers_from_history_without_new_fan_input(supervised):
 w,env,_,_,_=supervised
 pilot,seen,_,_=setup_history(w,env,recent=True)
 polls(w,pilot)
 assert len(w.fans.posts)==2 and len(seen)==2

@pytest.mark.parametrize('supervised',['autonomous'],indirect=True)
def test_historical_opened_flag_is_not_present_access_or_payment_authority(supervised):
 # Deliberately script the overclaim. The original positive expectation and
 # failed evidence remain preserved externally; old isOpened is not current entitlement.
 w,env,_,_,_=supervised
 pilot,seen,_,_=setup_history(w,env,purchased=True)
 send_http(pilot,24320,'Can I open the photo?')
 send_http(pilot,24321,'Can we talk about astronomy?',fan=FAN2)
 polls(w,pilot)
 assert not cards(w)
 assert len(w.fans.posts)==1
 assert 'astronomy' in w.fans.posts[0]['body']['text']
 assert not any('You have access' in post['body']['text'] for post in w.fans.posts)
 with Session(w.database[1]) as s:
  assert not list(s.scalars(select(Transaction)))
  historic=s.get(Message,(ACCOUNT,FAN,30999))
  assert historic is not None and historic.price_cents==300 and not historic.is_paid
