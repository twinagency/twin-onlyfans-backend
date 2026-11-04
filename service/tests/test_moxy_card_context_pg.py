"""The real snapshot exposes an older card when asked about it; no provider calls."""
import json
from datetime import timedelta
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_moxy_integrated_pg import setup,decision
from test_onlyfans_text_delivery_pg import TENANT,ACCOUNT
from db.models import Message
from db.onlyfans_moxy_models import MoxyEvent
from onlyfans_runtime.store import utcnow
from moxy_replacement.runtime import snapshot,validate_decision
from moxy_replacement.writer import messages


def test_question_about_old_unpaid_card_uses_durable_context(database,monkeypatch):
 engine,store,service,_,_,settings=setup(database,monkeypatch)
 now=utcnow()
 with Session(engine) as s,s.begin():
  s.add(Message(account_id=ACCOUNT,fan_id=101,message_id=10,direction='out',body='Old sports outfit card',price_cents=7500,is_paid=False,media_ids='[7101]',media_count=1,created_at=now-timedelta(days=3)))
  for i in range(105):s.add(Message(account_id=ACCOUNT,fan_id=101,message_id=100+i,direction='in',body='ordinary chat',created_at=now-timedelta(days=2,seconds=-i)))
  s.add(Message(account_id=ACCOUNT,fan_id=101,message_id=999,direction='in',body='What was in that old card, message 10? I have not paid for it. No new card please.',created_at=now))
 identity=service.add_event(TENANT,ACCOUNT,101,'turn','older-card-question',{'source_message_id':999})
 with Session(engine) as s,s.begin():
  snap=snapshot(s,ACCOUNT,101,s.get(MoxyEvent,(ACCOUNT,identity)))
  assert all(m['id']!=10 for m in snap['history'])
  assert snap['commercial_state']['last_offer']['message_id']==10
  assert snap['commercial_state']['sent_unpaid_cards'][0]['message_id']==10
  assert snap['fan_state']['purchased_products']==[]
  assert 'old' not in snap['commercial_state'].get('verified_purchases',[])
  prompt=json.loads(messages(snap)[1]['content'])
  assert prompt['commercial_state']['sent_unpaid_cards'][0]['message_id']==10
  assert prompt['commercial_state']['current_input']['id']==999
  reply=decision(text='The sports outfit photo in that old card is still locked and waiting for you.')
  validate_decision(settings,snap,reply,planning=True)
  assert reply['action']=='reply' and reply['product_id'] is None
 store.close()
