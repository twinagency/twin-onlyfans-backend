"""No-send input state, receipt-backed resolution and current readiness."""
import json
import pytest
from sqlalchemy.orm import Session
from sqlalchemy import select,func
from test_onlyfans_foundation_pg import database,event
from test_moxy_integrated_pg import setup,run,decision,model,status
from test_onlyfans_text_delivery_pg import TENANT,ACCOUNT,unthrottle
from db.models import VaultItem
from db.onlyfans_moxy_models import MoxyFan,MoxyEvent
from onlyfans_runtime.store import Refused,utcnow
from onlyfans_runtime.text_delivery import SyntheticTextTransport
from moxy_replacement.controls import view,update_fan


def evidenced_decision(store,*args,**kwargs):
    from test_moxy_behaviour_repair_pg import incoming,evidence
    flags=kwargs.get('flags',[])
    kinds={'hardship':'financial_hardship','ordinary_chat':'ordinary_chat_only',
           'refusal':'sales_refused','boundary_reset':'boundary_hold'}
    quotes={'hardship':'I cannot afford groceries.', 'ordinary_chat':'Just chat, no selling please.',
            'refusal':'Do not send me any more offers.', 'boundary_reset':'Are we a real couple outside this page?'}
    if flags:
        flag=flags[0];incoming(store,quotes[flag])
        kwargs['observations']=[evidence(kinds[flag],quotes[flag])]
    return decision(*args,**kwargs)

@pytest.mark.parametrize('flag,key',[('hardship','financial_hardship'),('ordinary_chat','ordinary_chat_only'),('refusal','sales_refused'),('boundary_reset','boundary_hold')])
def test_no_send_retains_input_constraint_and_blocks_later_sale(database,monkeypatch,flag,key):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    identity,_=run(service,engine,evidenced_decision(store,'none','',flags=[flag]))
    assert status(engine,identity)=='done'
    with Session(engine) as s:assert json.loads(s.get(MoxyFan,(ACCOUNT,101)).state_json)[key] is True
    later,_=run(service,engine,evidenced_decision(store,'offer','Fixture offer','first',sequence='fixture_level'),key='later')
    assert status(engine,later)=='held'
    store.close()


@pytest.mark.parametrize('flag,key',[('hardship','financial_hardship'),('ordinary_chat','ordinary_chat_only'),('refusal','sales_refused'),('boundary_reset','boundary_hold')])
@pytest.mark.parametrize('mode',['on','preview'])
def test_refused_sale_retains_constraint_only_outside_preview(database,monkeypatch,flag,key,mode):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch,mode=mode)
    identity,_=run(service,engine,evidenced_decision(store,'offer','This proposal must be refused.','first',
        flags=[flag],sequence='fixture_level'))
    assert status(engine,identity)=='held'
    with pytest.raises(Refused):service.queue(TENANT,ACCOUNT,identity)
    with Session(engine) as s:
        state=json.loads(s.get(MoxyFan,(ACCOUNT,101)).state_json)
        assert state.get(key) is (True if mode=='on' else None)
        assert s.get(MoxyEvent,(ACCOUNT,identity)).reason==('sales_state_held:ordinary_chat_only' if flag=='ordinary_chat' else 'sales_paused_by_current_input')
    if mode=='on':
        later,_=run(service,engine,evidenced_decision(store,'offer','A later proposal.','first',sequence='fixture_level'),key='later')
        assert status(engine,later)=='held'
    store.close()


def test_invalid_question_cannot_create_obligation_but_keeps_refusal(database,monkeypatch):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    identity,_=run(service,engine,evidenced_decision(store,'none','',flags=['refusal'],question='Invented buying question'))
    # An unquoted question is dropped (no reminder) rather than holding the whole turn.
    assert status(engine,identity)=='done'
    with Session(engine) as s:
        state=json.loads(s.get(MoxyFan,(ACCOUNT,101)).state_json)
        assert state['sales_refused'] is True and state.get('obligations',[])==[]
    store.close()


def test_unanswered_question_survives_no_send_and_only_receipt_resolves_it(database,monkeypatch):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    question='Is this a photo or a video?'
    store.record(TENANT,ACCOUNT,event(mid=2,body=question));store.drain(TENANT,ACCOUNT)
    first,_=run(service,engine,evidenced_decision(store,'none','',question=question))
    answer,_=run(service,engine,evidenced_decision(store,text='It is a photograph.',resolve_obligation=first),key='answer')
    intent=service.queue(TENANT,ACCOUNT,answer)
    def obligation():
        with Session(engine) as s:return json.loads(s.get(MoxyFan,(ACCOUNT,101)).state_json)['obligations'][0]
    assert obligation()['status']=='open'
    assert authority.dispatch(TENANT,ACCOUNT,intent,SyntheticTextTransport(ACCOUNT))=='accepted'
    assert obligation()['status']=='fulfilled' and obligation()['message_id']==9001
    store.close()


def test_reply_cannot_close_paid_service_obligation(database,monkeypatch):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    update_fan(store,TENANT,ACCOUNT,101,state={'obligations':[{'id':'owed','kind':'custom','status':'open','owner':'fixture','source_ref':'fixture:payment','product_id':'first'}]},actor='fixture',evidence_ref='fixture:proof')
    identity,_=run(service,engine,evidenced_decision(store,text='I will send it later.',resolve_obligation='owed'))
    assert status(engine,identity)=='held'
    store.close()


def test_preview_never_persists_constraints_or_questions(database,monkeypatch):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch,mode='preview')
    identity,_=run(service,engine,evidenced_decision(store,'none','',flags=['refusal']))
    assert status(engine,identity)=='preview'
    with Session(engine) as s:assert json.loads(s.get(MoxyFan,(ACCOUNT,101)).state_json)=={}
    store.close()


def test_readiness_reflects_asset_change_without_configuration_write(database,monkeypatch):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    assert view(store,TENANT,ACCOUNT)['readiness']['ready']
    with Session(engine) as s,s.begin():s.get(VaultItem,(ACCOUNT,7101)).removed_at=utcnow()
    result=view(store,TENANT,ACCOUNT)
    assert result['revision']==1 and not result['readiness']['ready']
    assert 'real_inventory_mapping_incomplete' in result['readiness']['blockers']
    assert service.configure(TENANT,ACCOUNT,expected_revision=1,mode='off',settings=settings,actor='fixture')['mode']=='off'
    store.close()


@pytest.mark.parametrize('origin',['campaign','engagement'])
def test_old_scheduler_cannot_create_unowned_moxy_events(database,monkeypatch,origin):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    from moxy_replacement.routing import route_legacy
    with pytest.raises(Refused,match='moxy_canonical_scheduler_required'):
        route_legacy(authority,TENANT,ACCOUNT,101,origin,'old-key',{})
    with Session(engine) as s:assert s.scalar(select(func.count()).select_from(MoxyEvent))==0
    store.close()

