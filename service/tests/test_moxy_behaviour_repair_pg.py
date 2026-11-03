import json
import pytest
from sqlalchemy.orm import Session
from db.onlyfans_moxy_models import MoxyEvent,MoxyFan
from test_onlyfans_foundation_pg import database,event
from test_onlyfans_text_delivery_pg import TENANT,ACCOUNT,unthrottle
from test_moxy_integrated_pg import setup,decision,run,status,model
from onlyfans_runtime.text_delivery import SyntheticTextTransport
from moxy_replacement.configuration import load,ActionBlocked
from moxy_replacement import writer


def evidence(kind,quote,*,active=True,product_id=None,mid=2):
    return dict(kind=kind,active=active,message_id=mid,quote=quote,product_id=product_id)


def incoming(store,text,mid=2):
    store.record(TENANT,ACCOUNT,event(mid=mid,body=text));store.drain(TENANT,ACCOUNT)


def test_affection_exception_is_in_effective_rules():
    rules={r['key']:r['instruction'] for r in load('amelia').public_context()['rules']}
    assert 'alone' in rules['boundary_reset'] and 'not a reason to go cold' in rules['relationships']


def test_casual_reply_label_does_not_create_no_sales_preference(database,monkeypatch):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    first,_=run(service,engine,decision(flags=['ordinary_chat']))
    with Session(engine) as s:assert not json.loads(s.get(MoxyFan,(ACCOUNT,101)).state_json).get('ordinary_chat_only')
    # Discarding the unposted reply is not equivalent to accepting a sale.
    with Session(engine) as s,s.begin():s.get(MoxyEvent,(ACCOUNT,first)).status='stale'
    next_id,_=run(service,engine,decision('offer','Here is the requested outfit.','first',sequence='fixture_level'),key='next')
    assert status(engine,next_id)=='ready'
    store.close()


@pytest.mark.parametrize('kind,quote',[
 ('ordinary_chat_only','Just chat with me, no offers please.'),
 ('sales_refused','Do not offer me any more content.'),
 ('boundary_hold','Are we actually a couple outside this site?'),
 ('financial_hardship','I cannot afford food this week.')])
def test_evidenced_constraints_persist_and_block_later_offers(database,monkeypatch,kind,quote):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    incoming(store,quote)
    first,_=run(service,engine,decision('none','',observations=[evidence(kind,quote)]))
    assert status(engine,first)=='done'
    with Session(engine) as s:
        state=json.loads(s.get(MoxyFan,(ACCOUNT,101)).state_json)
        assert state[kind] is True and state['constraint_evidence'][kind]['message_id']==2
        if kind=='boundary_hold':assert not state.get('sales_refused',False)
    later,_=run(service,engine,decision('offer','A later offer.','first',sequence='fixture_level'),key='later')
    assert status(engine,later)=='held'
    store.close()


def test_invented_quote_cannot_create_a_fan_constraint(database,monkeypatch):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    identity,_=run(service,engine,decision('none','',observations=[evidence('sales_refused','Invented refusal')]))
    assert status(engine,identity)=='held'
    with Session(engine) as s:assert json.loads(s.get(MoxyFan,(ACCOUNT,101)).state_json)=={}
    store.close()


def test_item_refusal_does_not_block_different_item(database,monkeypatch):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    incoming(store,'Not that outfit photo, show me a different one.')
    first,_=run(service,engine,decision('none','',observations=[evidence('sales_refused','Not that outfit photo',product_id='first')]))
    with Session(engine) as s:
        state=json.loads(s.get(MoxyFan,(ACCOUNT,101)).state_json)
        assert state['rejected_products']==['first'] and not state.get('sales_refused',False)
    later,_=run(service,engine,decision('offer','A different outfit photo.','second',sequence=None),key='different')
    assert status(engine,later)=='held'
    with Session(engine) as s:assert s.get(MoxyEvent,(ACCOUNT,later)).reason=='sequence_context_missing'
    # The independently applicable fan restriction is item-specific.
    from moxy_replacement.runtime import configuration,snapshot
    with Session(engine) as s:
        snap=snapshot(s,ACCOUNT,101,s.get(MoxyEvent,(ACCOUNT,later)))
    pa=configuration(settings)[1];pa.sequences={}
    pa.terms('second',snap['fan_state'])
    with pytest.raises(ActionBlocked,match='product_previously_rejected'):pa.terms('first',snap['fan_state'])
    store.close()


@pytest.mark.parametrize('interruption',[None,'input','takeover','asset'])
def test_ordered_text_parts_revalidate_without_replaying_first(database,monkeypatch,interruption):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    parts=[{'text':'Training finished?','media':False},{'text':'How did the last set go?','media':False}]
    identity,_=run(service,engine,decision(text=parts[0]['text'],messages=parts))
    first=service.queue(TENANT,ACCOUNT,identity)
    assert authority.dispatch(TENANT,ACCOUNT,first,SyntheticTextTransport(ACCOUNT,message_id=9001))=='accepted'
    assert service.queue(TENANT,ACCOUNT,identity)!=first
    second=service.queue(TENANT,ACCOUNT,identity)
    if interruption=='input':incoming(store,'Actually, wait.',mid=9003)
    elif interruption:
        from db.models import OfConversationState,VaultItem
        from onlyfans_runtime.store import utcnow
        with Session(engine) as s,s.begin():
            if interruption=='takeover':s.get(OfConversationState,(ACCOUNT,101)).mode='human_controlled'
            if interruption=='asset':s.get(VaultItem,(ACCOUNT,7101)).removed_at=utcnow()
    wire=SyntheticTextTransport(ACCOUNT,message_id=9002)
    unthrottle(engine)
    result=authority.dispatch(TENANT,ACCOUNT,second,wire)
    if interruption:assert result in {'cancelled','refused'} and wire.calls==[]
    else:
        assert result=='accepted' and wire.calls[0].body==parts[1]['text']
        assert status(engine,identity)=='done'
        with Session(engine) as s:
            progress=json.loads(s.get(MoxyEvent,(ACCOUNT,identity)).payload_json)['_moxy_delivery']
            assert len(progress['receipts'])==2
    store.close()


@pytest.mark.parametrize('parts',[
 [{'text':'First','media':False},{'text':'<internal>','media':False}],
 [{'text':'First','media':False},{'text':'See https://example.com','media':False}],
 [{'text':'First','media':True},{'text':'Again','media':True}],
])
def test_every_message_part_is_validated(parts):
    with pytest.raises(ActionBlocked):writer.parse(json.dumps(decision(text='First',messages=parts)))


def test_text_card_text_keeps_one_card_and_correct_price(database,monkeypatch):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    parts=[{'text':'Here is the outfit you asked for.','media':False},
           {'text':'The selected outfit photograph.','media':True},
           {'text':'Which detail caught your eye?','media':False}]
    identity,_=run(service,engine,decision('offer',parts[0]['text'],'first',sequence='fixture_level',messages=parts))
    seen=[]
    for offset,part in enumerate(parts):
        unthrottle(engine)
        intent=service.queue(TENANT,ACCOUNT,identity);seen.append(intent)
        wire=SyntheticTextTransport(ACCOUNT,message_id=9001+offset)
        assert authority.dispatch(TENANT,ACCOUNT,intent,wire)=='accepted'
        assert wire.calls[0].body==part['text']
        assert wire.calls[0].media_ids==((7101,) if part['media'] else ())
        assert wire.calls[0].price_cents==(7500 if part['media'] else 0)
    assert len(set(seen))==3 and status(engine,identity)=='done'
    store.close()


@pytest.mark.parametrize('qualification',[True,False])
def test_paid_qualification_has_no_live_fan_event_or_delivery(database,monkeypatch,qualification):
    import asyncio
    from datetime import datetime,timedelta,timezone
    from sqlalchemy import select,func
    from db.models import AccountAiConfig,GrokCall
    from db.onlyfans_delivery_models import OfTextIntent,OfNativePlan
    from onlyfans_runtime.provider_model import NativeProviderModel
    from onlyfans_runtime.store import utcnow
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    m=NativeProviderModel(account_id=ACCOUNT,model_id='grok-4-1-fast-non-reasoning',api_key='fixture-not-a-real-key',
        expires_at=datetime.now(timezone.utc)+timedelta(hours=1))
    prompts=[{'role':'user','content':'Synthetic noncustomer qualification.'}]
    with Session(engine) as s,s.begin():
        cfg=s.get(AccountAiConfig,ACCOUNT);cfg.daily_cost_cap_cents=10000
        config={c.name:getattr(cfg,c.name) for c in cfg.__table__.columns}
        configure=m.configure_qualification if qualification else m.configure_call
        configure(account_id=ACCOUNT,chosen_model=m.model_id,config=config,messages=prompts)
        call,units,day=m.reserve(s,account_id=ACCOUNT,fan_id=101,model=m.model_id,messages=prompts,now=utcnow())
        call_id=call.id
    async def fake_post(body):
        return json.dumps({'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':'{"fixture":true}'}}],
            'usage':{'prompt_tokens':10,'completion_tokens':8}}).encode()
    monkeypatch.setattr(m,'_post_once',fake_post)
    if qualification:
        assert asyncio.run(m.generate(prompts))=='{"fixture":true}'
        with Session(engine) as s,s.begin():
            m.settle(s,account_id=ACCOUNT,call_id=call_id,reserved_millicents=units,
                actual_millicents=m.reported_cost_millicents,now=utcnow())
            assert s.get(GrokCall,call_id).purpose=='moxy_qualification'
    else:
        with pytest.raises(Exception,match='committed scoped plan evidence'):asyncio.run(m.generate(prompts))
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(MoxyEvent))==0
        assert s.scalar(select(func.count()).select_from(OfNativePlan))==0
        assert s.scalar(select(func.count()).select_from(OfTextIntent))==0
        assert json.loads(s.get(MoxyFan,(ACCOUNT,101)).state_json)=={}
    store.close()


@pytest.mark.parametrize('action',['none','handover'])
def test_empty_message_plan_represents_no_outbound_delivery(action):
    result=writer.parse(json.dumps(decision(action,'',messages=[])))
    from moxy_replacement.parts import plan
    assert plan(result)==[]
    with pytest.raises(ActionBlocked):
        writer.parse(json.dumps(decision(action,'',messages=[{'text':'Must not send','media':False}])))
