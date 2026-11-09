import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_engine_real_worker_pg import world
from test_plan_c_draft_app_pg import configured_draft
from test_plan_c_supervised_pg import supervised
from test_offline_completion_pg import normal_pilot
from test_plan_c_sales_journey_pg import writer,cards
from test_plan_c_nonempty_history_pg import send_http,polls
from test_onlyfans_text_delivery_pg import ACCOUNT,FAN
from db.onlyfans_engine_models import OfEngineOwner
@pytest.mark.parametrize('supervised',['autonomous'],indirect=True)
def test_paused_sales_can_answer_price_without_reopening_or_sending_card(supervised):
 w,env,_,_,_=supervised
 normal_pilot(w,env,selecting=False)
 pilot,seen,_=writer(w,env,choose=lambda latest,original:original if latest=='no thanks' else {'body':'The photo is USD 3.00.'})
 send_http(pilot,94001,'no thanks');polls(w,pilot)
 with Session(w.database[1]) as s:assert s.get(OfEngineOwner,(ACCOUNT,FAN)).sales_mode=='paused'
 send_http(pilot,94002,'How much is the photo?');polls(w,pilot)
 assert len(w.fans.posts)==2 and 'USD 3.00' in w.fans.posts[-1]['body']['text']
 assert not cards(w)
 with Session(w.database[1]) as s:assert s.get(OfEngineOwner,(ACCOUNT,FAN)).sales_mode=='paused'


@pytest.mark.parametrize('supervised',['autonomous'],indirect=True)
def test_listing_fact_is_read_only_scoped_and_unavailable_for_changed_product(supervised,monkeypatch):
 from types import SimpleNamespace
 from conversation_engine.engine import _price_information
 from conversation_engine.writer_context import CreatorProfile
 from db.models import CatalogItem,VaultItem
 from db.onlyfans_engine_models import OfEngineOfferGrant
 from conversation_engine import pilot_scope
 from onlyfans_runtime.store import utcnow,Refused
 w,env,_,_,_=supervised
 normal_pilot(w,env,selecting=False)
 from test_onlyfans_text_delivery_pg import TENANT
 before=pilot_scope.describe(w.store,TENANT,ACCOUNT)
 with Session(w.database[1]) as s,s.begin():
  runtime=w.store._runtime(s,TENANT,ACCOUNT,lock=False)
  info=_price_information(s,runtime)
  assert len(info)==1 and info[0]['priceCents']==300 and info[0]['currency']=='USD'
  assert 'grantId' not in info[0] and not list(s.scalars(select(OfEngineOfferGrant)))
  with pytest.raises(Refused,match='pilot creator binding changed'):
   _price_information(s,SimpleNamespace(account_id=ACCOUNT,binding_id='foreign-binding'))
  for model,key,value in [(s.get(CatalogItem,w.item),'price_cents',400),
                           (s.get(VaultItem,(ACCOUNT,11)),'removed_at',utcnow())]:
   point=s.begin_nested();setattr(model,key,value);s.flush()
   assert _price_information(s,runtime)==[]
   point.rollback()
  with monkeypatch.context() as patch:
   patch.setattr('conversation_engine.writer_context.active_profile',
    lambda *args,**kwargs:CreatorProfile(name='Fixture',offers_permitted=False))
   assert _price_information(s,runtime)==[]
  assert not list(s.scalars(select(OfEngineOfferGrant)))
 after=pilot_scope.describe(w.store,TENANT,ACCOUNT)
 assert (after['providerRequestsUsed'],after['providerCreditsReserved'])==(before['providerRequestsUsed'],before['providerCreditsReserved'])
 assert not w.fans.posts

@pytest.mark.parametrize('supervised',['autonomous'],indirect=True)
def test_listing_price_changed_during_model_call_cannot_reach_provider(supervised):
 from db.models import CatalogItem
 w,env,_,_,_=supervised
 normal_pilot(w,env,selecting=False)
 changed=False
 def on_call(latest,prompt):
  nonlocal changed
  if latest=='How much is the photo?' and not changed:
   with Session(w.database[1]) as s,s.begin():s.get(CatalogItem,w.item).price_cents=400
   changed=True
 pilot,seen,_=writer(w,env,on_call=on_call,choose=lambda latest,original:original if latest=='no thanks' else {'body':'The photo is USD 3.00.'})
 send_http(pilot,94101,'no thanks');polls(w,pilot)
 sent=len(w.fans.posts)
 send_http(pilot,94102,'How much is the photo?');polls(w,pilot)
 assert changed and len(w.fans.posts)==sent and not cards(w)
 with Session(w.database[1]) as s:assert s.get(OfEngineOwner,(ACCOUNT,FAN)).sales_mode=='paused'
