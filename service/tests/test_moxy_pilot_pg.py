"""Supervised pilot constraints using labelled fixture content, never customers."""
import json
import pytest
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_moxy_integrated_pg import setup,run,decision,model
from test_onlyfans_text_delivery_pg import TENANT,ACCOUNT,unthrottle
from db.models import Fan,OfConversationState,Message
from db.onlyfans_moxy_models import MoxyEvent,MoxyFan
from db.onlyfans_case_models import OfOperatorCase
from onlyfans_runtime.text_delivery import SyntheticTextTransport


def pilot(database,monkeypatch,creator='amelia',mode='on'):
    values=setup(database,monkeypatch,creator,mode=mode)
    engine,store,service,authority,claim,settings=values
    settings['pilot']={'fan_ids':[101],'product_ids':['first','second','third','fourth'],'supervisor':'fixture-operator'}
    service.configure(TENANT,ACCOUNT,expected_revision=1,mode=mode,settings=settings,actor='fixture-operator')
    return values


@pytest.mark.parametrize('creator',['amelia','cristina'])
def test_pilot_keeps_proactive_invitation_and_paid_media(database,monkeypatch,creator):
    engine,store,service,authority,claim,settings=pilot(database,monkeypatch,creator)
    identity,_=run(service,engine,decision('invite','Want the sports outfit photo?','first',sequence='fixture_level'))
    intent=service.queue(TENANT,ACCOUNT,identity)
    assert authority.dispatch(TENANT,ACCOUNT,intent,SyntheticTextTransport(ACCOUNT))=='accepted'
    from test_onlyfans_foundation_pg import event
    store.record(TENANT,ACCOUNT,event(mid=9100,body='Yes please, send that outfit'));store.drain(TENANT,ACCOUNT)
    second,_=run(service,engine,decision('offer','Here is the sports outfit.','first',sequence='fixture_level',
        request_kind='accept',request_quote='Yes please, send that outfit'),key='acceptance')
    unthrottle(engine)
    paid=service.queue(TENANT,ACCOUNT,second);wire=SyntheticTextTransport(ACCOUNT,message_id=9101)
    assert authority.dispatch(TENANT,ACCOUNT,paid,wire)=='accepted'
    assert wire.calls[0].price_cents==7500 and wire.calls[0].media_ids==(7101,)
    store.close()


@pytest.mark.parametrize('kind',['campaign','scheduled','fulfilment'])
def test_deferred_automation_cannot_call_model(database,monkeypatch,kind):
    engine,store,service,authority,claim,settings=pilot(database,monkeypatch)
    identity=service.add_event(TENANT,ACCOUNT,101,kind,'deferred',{})
    generator=model(decision())
    service.prepare(TENANT,ACCOUNT,identity,generator)
    with Session(engine) as s:assert s.get(MoxyEvent,(ACCOUNT,identity)).reason=='pilot_automation_deferred'
    assert generator.calls==[]
    store.close()


@pytest.mark.parametrize('mode',['on','preview'])
def test_specialist_request_routes_to_named_human_without_payment_or_fulfilment(database,monkeypatch,mode):
    engine,store,service,authority,claim,settings=pilot(database,monkeypatch,mode=mode)
    identity,_=run(service,engine,decision('offer','I can make a custom right now.','first',permission='customs',sequence='fixture_level'))
    with Session(engine) as s:
        row=s.get(MoxyEvent,(ACCOUNT,identity));saved=json.loads(row.decision_json)
        assert saved['action']=='handover' and saved['text']=='' and saved['product_id'] is None
        cases=s.scalars(select(OfOperatorCase)).all()
        assert len(cases)==(1 if mode=='on' else 0)
        if cases:assert cases[0].owner=='fixture-operator' and cases[0].category=='other'
        assert s.scalar(select(func.count()).select_from(Message).where(Message.direction=='out'))==0
    store.close()


def test_selected_fans_only_and_ten_fan_workload(database,monkeypatch):
    engine,store,service,authority,claim,settings=pilot(database,monkeypatch)
    fans=list(range(201,212))
    with Session(engine) as s,s.begin():
        for fan in fans:
            s.add(Fan(account_id=ACCOUNT,fan_id=fan));s.flush()
            s.add(OfConversationState(account_id=ACCOUNT,fan_id=fan,mode='automated',revision=0,input_revision=0,takeover_state='none'))
    settings['pilot']['fan_ids']=fans[:10]
    service.configure(TENANT,ACCOUNT,expected_revision=2,mode='on',settings=settings,actor='fixture-operator')
    excluded=service.add_event(TENANT,ACCOUNT,fans[10],'turn','excluded',{})
    generator=model(decision());service.prepare(TENANT,ACCOUNT,excluded,generator)
    assert generator.calls==[]
    identities=[service.add_event(TENANT,ACCOUNT,f,'turn','pilot:'+str(f),{}) for f in fans[:10]]
    import time
    start=time.monotonic()
    for _ in range(5):assert service.step(TENANT,ACCOUNT,generator,limit=2)==2
    for n,identity in enumerate(identities):
        unthrottle(engine)
        intent=service.queue(TENANT,ACCOUNT,identity)
        assert authority.dispatch(TENANT,ACCOUNT,intent,SyntheticTextTransport(ACCOUNT,message_id=20000+n))=='accepted'
    with Session(engine) as s:
        assert s.get(MoxyEvent,(ACCOUNT,excluded)).reason=='outside_pilot_recipient_scope'
        assert all(s.get(MoxyEvent,(ACCOUNT,i)).status=='done' for i in identities)
    print(json.dumps({'fixture_pilot_fans':10,'preparation_batch':2,'synthetic_seconds':round(time.monotonic()-start,3),'real_model':False,'real_provider':False}))
    store.close()


@pytest.mark.parametrize('gate',['wrong_tenant','stop','external_sender','off'])
def test_pilot_controls_prevent_generation_and_sending(database,monkeypatch,gate):
    from onlyfans_runtime.store import Refused
    engine,store,service,authority,claim,settings=pilot(database,monkeypatch)
    if gate=='wrong_tenant':
        with pytest.raises(Refused):service.add_event('another-tenant',ACCOUNT,101,'turn','wrong-tenant',{})
        store.close();return
    if gate=='stop':
        store.control(TENANT,ACCOUNT,101,mode='no_contact',expected_revision=0,
            command_key='pilot-stop',actor='fixture-operator',reason='Explicit test stop')
    elif gate=='external_sender':
        from onlyfans_runtime.operator_cases import open_case
        with Session(engine) as s,s.begin():open_case(s,ACCOUNT,101,'external_sender',reason='fixture_external_sender',
            evidence={'fixture':True},owner='fixture-operator',scope='all')
    else:service.configure(TENANT,ACCOUNT,expected_revision=2,mode='off',settings=settings,actor='fixture-operator')
    identity=service.add_event(TENANT,ACCOUNT,101,'turn','control-check',{})
    generator=model(decision())
    service.prepare(TENANT,ACCOUNT,identity,generator)
    assert generator.calls==[]
    with Session(engine) as s:
        assert s.get(MoxyEvent,(ACCOUNT,identity)).status=='held'
        assert s.scalar(select(func.count()).select_from(Message).where(Message.direction=='out'))==0
    store.close()
