"""Exact request from the paid-model evaluation, plus authority negative controls."""
import pytest
from sqlalchemy.orm import Session
from sqlalchemy import select,func
from db.onlyfans_delivery_models import OfTextIntent
from test_moxy_integrated_pg import setup,decision,model
from test_moxy_resend_repair_pg import prior_card,incoming
from test_moxy_reply_recovery_pg import add,row,send
from test_onlyfans_foundation_pg import database,event
from test_onlyfans_text_delivery_pg import ACCOUNT,TENANT

@pytest.mark.parametrize('text,allowed',[
 ('Send that exact card again please.',True),
 ('Can you resend the same card please?',True),
 ('Please send that one again.',True),
 ("Why can't you send that exact card again?",False),
 ('Do not send that exact card again.',False),
 ('Send that exact card again tomorrow.',False),
])
def test_exact_existing_card_request_reaches_one_delivery_then_next_input(database,monkeypatch,text,allowed):
    engine,store,service,authority,_,_=setup(database,monkeypatch)
    try:
        prior_card(engine,service,authority);incoming(store,text)
        identity=add(service,key='exact-resend',mid=2)
        service.prepare(TENANT,ACCOUNT,identity,model(decision('resend','Here is the outfit photo again.',
            'first',sequence='fixture_level',request_kind='resend',request_quote=text,unanswered_input_ids=[])))
        if not allowed:
            assert row(engine,identity).status=='held'
            with Session(engine) as s:assert s.scalar(select(func.count()).select_from(OfTextIntent))==1
            return
        assert row(engine,identity).status=='ready',row(engine,identity).reason
        result,wire,intent=send(service,authority,engine,identity,mid=9002)
        assert result=='accepted' and len(wire.calls)==1
        assert wire.calls[0].price_cents==7500 and wire.calls[0].media_ids==(7101,)
        assert authority.dispatch(TENANT,ACCOUNT,intent,wire)=='accepted'
        assert len(wire.calls)==1
        store.record(TENANT,ACCOUNT,event(mid=3,body='How was your afternoon?'));store.drain(TENANT,ACCOUNT)
        later=add(service,key='next',mid=3)
        service.prepare(TENANT,ACCOUNT,later,model(decision(text='What have you been up to?',unanswered_input_ids=[])))
        assert send(service,authority,engine,later,mid=9003)[0]=='accepted'
    finally:store.close()
