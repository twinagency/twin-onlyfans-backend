from concurrent.futures import ThreadPoolExecutor
from datetime import timezone
import json
import pytest
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database,event
from test_moxy_integrated_pg import setup,decision,model,run,status
from test_onlyfans_text_delivery_pg import TENANT,ACCOUNT,unthrottle
from db.models import Message,ContentOffer,OfConversationState,VaultItem
from db.onlyfans_delivery_models import OfTextIntent
from db.onlyfans_moxy_models import MoxyEvent,MoxyFan
from onlyfans_runtime.store import Refused,utcnow
from onlyfans_runtime.text_delivery import SyntheticTextTransport
from onlyfans_runtime.commerce import CommerceIntake,SyntheticCommerceEvent
from moxy_replacement.runtime import MoxyService
from moxy_replacement.controls import update_fan


def test_exact_purchase_continues_without_new_fan_input_and_reversal_removes_ownership(database,monkeypatch):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch,sequence=True)
    first,_=run(service,engine,decision('offer','Here is the requested outfit photo.','first',sequence='vault'))
    intent=service.queue(TENANT,ACCOUNT,first)
    assert authority.dispatch(TENANT,ACCOUNT,intent,SyntheticTextTransport(ACCOUNT))=='accepted'
    proof=SyntheticCommerceEvent(ACCOUNT,101,9001,'fixture-purchase','fixture-tx',1,'purchase',7500,'USD',
        utcnow().replace(tzinfo=timezone.utc).isoformat(),'fixture:exact-link')
    assert CommerceIntake(store).record(TENANT,ACCOUNT,proof)=='applied'
    assert CommerceIntake(store).record(TENANT,ACCOUNT,proof)=='applied'
    with Session(engine) as s:
        purchases=s.scalars(select(MoxyEvent).where(MoxyEvent.kind=='purchase')).all()
        assert len(purchases)==1
        purchase=purchases[0].event_id
    service.prepare(TENANT,ACCOUNT,purchase,model(decision('continue','The next outfit in the approved sequence.','second',sequence='vault')))
    next_intent=service.queue(TENANT,ACCOUNT,purchase);unthrottle(engine)
    wire=SyntheticTextTransport(ACCOUNT,message_id=9002)
    assert authority.dispatch(TENANT,ACCOUNT,next_intent,wire)=='accepted'
    assert wire.calls[0].media_ids==(7102,)
    reversal=SyntheticCommerceEvent(ACCOUNT,101,9001,'fixture-refund','fixture-tx',2,'refund',7500,'USD',
        utcnow().replace(tzinfo=timezone.utc).isoformat(),'fixture:exact-reversal')
    assert CommerceIntake(store).record(TENANT,ACCOUNT,reversal)=='applied'
    with Session(engine) as s:
        assert s.get(Message,(ACCOUNT,101,9001)).is_paid is False
        assert s.scalar(select(MoxyEvent).where(MoxyEvent.kind=='refund')) is not None
    store.close()


def test_accepted_invitation_is_durable_and_acceptance_selects_exact_product(database,monkeypatch):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    invite,_=run(service,engine,decision('invite','Want the sports outfit photo?','first',sequence='fixture_level'))
    intent=service.queue(TENANT,ACCOUNT,invite)
    assert authority.dispatch(TENANT,ACCOUNT,intent,SyntheticTextTransport(ACCOUNT))=='accepted'
    store.record(TENANT,ACCOUNT,event(mid=9002,body='Absolutely, send that one over'));store.drain(TENANT,ACCOUNT)
    identity,_=run(service,engine,decision('offer','Here it is.','first',sequence='fixture_level'),key='accepted-invite')
    assert status(engine,identity)=='ready'
    with Session(engine) as s:
        assert json.loads(s.get(MoxyFan,(ACCOUNT,101)).state_json)['invitation']['product_id']=='first'
    store.close()


def test_complimentary_correction_fulfils_owned_obligation_without_purchase(database,monkeypatch):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    update_fan(store,TENANT,ACCOUNT,101,state={'obligations':[{'id':'owed-image','status':'open','owner':'fixture-operator',
        'source_ref':'fixture:accepted-promise','product_id':'first'}]},actor='fixture-operator',evidence_ref='fixture:records')
    identity,_=run(service,engine,decision('fulfil','Here is the image that was still owed.','first',resolve_obligation='owed-image'))
    intent=service.queue(TENANT,ACCOUNT,identity)
    wire=SyntheticTextTransport(ACCOUNT)
    assert authority.dispatch(TENANT,ACCOUNT,intent,wire)=='accepted'
    assert wire.calls[0].price_cents==0
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(ContentOffer))==0
        assert s.get(Message,(ACCOUNT,101,9001)).is_paid is None
        assert json.loads(s.get(MoxyFan,(ACCOUNT,101)).state_json)['obligations'][0]['status']=='fulfilled'
    store.close()


@pytest.mark.parametrize('creator,spender,expected',[('amelia',False,'held'),('amelia',True,'ready'),('cristina',False,'ready')])
def test_creator_permission_conditions_execute_not_only_prompt(database,monkeypatch,creator,spender,expected):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch,creator)
    service.fan_facts(TENANT,ACCOUNT,101,{'spender':spender,'vip':spender},actor='fixture-operator',evidence_ref='fixture:verified-tier')
    identity,_=run(service,engine,decision(text='A labelled non-explicit fixture response.',permission='sexting'))
    assert status(engine,identity)==expected
    store.close()


@pytest.mark.parametrize('flag',['hardship','ordinary_chat','refusal','boundary_reset'])
def test_current_semantic_input_flags_prevent_paid_offer(database,monkeypatch,flag):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    identity,_=run(service,engine,decision('offer','This must be held.','first',flags=[flag],sequence='fixture_level'))
    assert status(engine,identity)=='held'
    with pytest.raises(Refused):service.queue(TENANT,ACCOUNT,identity)
    store.close()


def test_two_workers_reserve_one_generation_for_same_event(database,monkeypatch):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    identity=service.add_event(TENANT,ACCOUNT,101,'scheduled','fixture-concurrent',{})
    models=[model(decision(),delay_seconds=.1),model(decision(),delay_seconds=.1)]
    def execute(i):return MoxyService(store).prepare(TENANT,ACCOUNT,identity,models[i])
    with ThreadPoolExecutor(2) as pool:assert list(pool.map(execute,[0,1]))==[identity,identity]
    assert sum(len(m.calls) for m in models)==1
    store.close()


def test_missing_attachment_blocks_only_affected_offer_and_does_not_block_chat(database,monkeypatch):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    with Session(engine) as s,s.begin():s.get(VaultItem,(ACCOUNT,7101)).removed_at=utcnow()
    identity,_=run(service,engine,decision('offer','Held missing media.','first',sequence='fixture_level'))
    assert status(engine,identity)=='held'
    reply,_=run(service,engine,decision(),key='unrelated-chat')
    assert status(engine,reply)=='ready'
    store.close()
