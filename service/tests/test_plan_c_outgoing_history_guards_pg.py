import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_engine_real_worker_pg import world,iso,CREATOR,FAN2
from test_onlyfans_text_delivery_pg import TENANT,ACCOUNT,FAN
from db.models import Message,Transaction,OfConversationState
from db.onlyfans_models import OfIngress
from db.onlyfans_engine_models import OfEngineOfferGrant
from onlyfans_runtime.store import Refused

def row(**changes):
 result={'id':48001,'fromUser':{'id':int(CREATOR)},'isSentByMe':True,'text':'Earlier free photo',
         'createdAt':iso(-60),'price':0,'media':[{'id':13,'type':'photo'}]}
 result.update(changes);return result

def test_outgoing_history_context_is_idempotent_and_never_action_or_ownership(world):
 item=row()
 world.intake.ingest_history_item(TENANT,ACCOUNT,FAN,item)
 world.intake.ingest_history_item(TENANT,ACCOUNT,FAN,item)
 world.store.drain(TENANT,ACCOUNT)
 with Session(world.database[1]) as s:
  message=s.get(Message,(ACCOUNT,FAN,item['id']))
  assert message.direction=='out' and message.price_cents==0 and not message.is_paid
  assert len(list(s.scalars(select(OfIngress))))==1
  assert s.scalar(select(OfIngress)).source=='import'
  control=s.get(OfConversationState,(ACCOUNT,FAN))
  assert control.mode=='automated' and control.takeover_state=='none'
  assert not list(s.scalars(select(OfEngineOfferGrant)))
  assert not list(s.scalars(select(Transaction)))
 assert not world.fans.posts and not world.snaps

@pytest.mark.parametrize('changes',[
 {'fromUser':{'id':999}},
 {'fromUser':{'id':FAN},'isSentByMe':True},
 {'isSentByMe':False},
 {'fromUser':{},'isSentByMe':None},
 {'price':False},
 {'price':-1},
 {'media':[None]},
 {'text':123},
])
def test_malformed_or_wrong_creator_outgoing_history_is_not_context(world,changes):
 with pytest.raises(Refused):world.intake.ingest_history_item(TENANT,ACCOUNT,FAN,row(**changes))
 with Session(world.database[1]) as s:
  assert not list(s.scalars(select(OfIngress)))
  assert s.get(Message,(ACCOUNT,FAN,48001)) is None

def test_outgoing_history_conflict_retains_original_and_hold(world):
 item=row()
 world.intake.ingest_history_item(TENANT,ACCOUNT,FAN,item);world.store.drain(TENANT,ACCOUNT)
 world.intake.ingest_history_item(TENANT,ACCOUNT,FAN,{**item,'text':'Changed creator text'})
 world.store.drain(TENANT,ACCOUNT)
 with Session(world.database[1]) as s:
  assert s.get(Message,(ACCOUNT,FAN,item['id'])).body=='Earlier free photo'
  assert s.get(OfConversationState,(ACCOUNT,FAN)).requires_attention
  assert any(x.status=='quarantined' for x in s.scalars(select(OfIngress)))
 assert not world.fans.posts

def test_outgoing_history_wrong_tenant_cannot_write(world):
 with pytest.raises(Refused):world.intake.ingest_history_item('foreign-tenant',ACCOUNT,FAN,row())
 with Session(world.database[1]) as s:assert not list(s.scalars(select(OfIngress)))
