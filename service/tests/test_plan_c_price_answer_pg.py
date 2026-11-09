import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient
from test_onlyfans_foundation_pg import database
from test_engine_real_worker_pg import world,inbound
from test_plan_c_draft_app_pg import configured_draft,signed
from test_plan_c_supervised_pg import supervised
from test_offline_completion_pg import normal_pilot
from test_plan_c_sales_journey_pg import writer
from test_onlyfans_text_delivery_pg import ACCOUNT
from db.onlyfans_engine_models import OfEngineOfferGrant,OfEngineRequest
from plan_c_worker import create_app

@pytest.mark.parametrize('supervised',['autonomous'],indirect=True)
@pytest.mark.parametrize('question',[
 'How much is the photo?',
 'What does the photo cost?',
 'Can you send the price for the photo?',
 'How much would it cost to buy the photo?',
])
def test_price_question_gets_verified_answer_without_a_paid_card(supervised,question):
 w,env,_,_,_=supervised
 normal_pilot(w,env,selecting=False)
 with Session(w.database[1]) as s:assert not list(s.scalars(select(OfEngineOfferGrant)))
 pilot,seen,_=writer(w,env,choose=lambda latest,original:{'body':'The photo is USD 3.00.'})
 raw,headers=signed(inbound(24200,question),key='price-answer-required')
 with TestClient(create_app(pilot,run_loop=False)) as client:
  assert client.post('/onlyfans/webhooks/plan-c',content=raw,headers=headers).status_code==202
 assert not seen and not w.fans.posts
 pilot.tick()
 assert seen and 'APPROVED OFFER: id ' in seen[0]['messages'][-1]['content']
 assert len(w.fans.posts)==1,'a truthful approved-price answer must complete, not strand the question'
 assert 'USD 3.00' in w.fans.posts[0]['body']['text']
 assert not w.fans.posts[0]['body'].get('price',0),'a price question alone must not send the card'
 with Session(w.database[1]) as s:
  request=s.scalar(select(OfEngineRequest).where(OfEngineRequest.source_message_id==24200))
  assert request.status=='completed' and request.next_step=='none'
 assert len(seen)==1
