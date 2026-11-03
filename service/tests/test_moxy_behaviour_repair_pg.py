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


def test_paraphrased_request_quote_on_plain_reply_is_sent_not_held(database,monkeypatch):
    # Live Amelia/Gervonta 2026-11-03: a decline reply quoting an older message was held and the fan got silence.
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    incoming(store,'I came here to see your pussy not talk about all this random stuff')
    identity,_=run(service,engine,decision('reply','not my thing, but I have something you will like.',
        request_kind='direct',request_quote='Video of your pussy'))
    assert status(engine,identity)=='ready'
    with Session(engine) as s:assert 'direct_request_product' not in json.loads(s.get(MoxyFan,(ACCOUNT,101)).state_json)
    store.close()


def test_paraphrased_request_quote_still_blocks_commerce(database,monkeypatch):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    incoming(store,'I came here to see your pussy not talk about all this random stuff')
    identity,_=run(service,engine,decision('offer','Here is the closest thing I have.','first',sequence='fixture_level',
        request_kind='direct',request_quote='Video of your pussy'))
    assert status(engine,identity)=='held'
    store.close()


def test_request_quote_tolerates_case_spacing_and_typographic_quotes(database,monkeypatch):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    incoming(store,'I’m  keen, send   the OUTFIT photo')
    identity,_=run(service,engine,decision('offer','Here it is.','first',sequence='fixture_level',
        request_kind='direct',request_quote="i'm keen, send the outfit photo"))
    assert status(engine,identity)=='ready'
    store.close()


def test_writer_prompt_follows_innuendo_and_pivots_unavailable_requests():
    from moxy_replacement.configuration import load as _load
    snap={'instructions':_load('amelia').public_context(),'pilot':True}
    import moxy_replacement.writer as w
    text=w.messages.__code__.co_consts
    joined=' '.join(c for c in text if isinstance(c,str))
    assert 'never resend or lightly reword a line already sent' in joined
    assert 'relevant eligible item exists' in joined and 'MAY talk about' in joined and 'commercial_state' in joined
    assert 'character-for-character' in joined


def test_paraphrased_buying_question_drops_reminder_not_the_sale(database,monkeypatch):
    # Live qualification 2026-11-03: a correct decline + real alternative was held by question_requires_source_quote.
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    incoming(store,'I came here to see your pussy not talk about all this random stuff')
    identity,_=run(service,engine,decision('offer','Not that, but this one is close.','first',sequence='fixture_level',
        question='Video of your pussy'))
    assert status(engine,identity)=='ready'
    with Session(engine) as s:assert json.loads(s.get(MoxyEvent,(ACCOUNT,identity)).decision_json)['question'] is None
    store.close()


@pytest.mark.parametrize('kind,expected',[('buying_question','ready'),('owed','held')])
def test_offer_may_answer_a_buying_question_but_never_closes_an_owed_item(database,monkeypatch,kind,expected):
    from moxy_replacement.controls import update_fan
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    ob={'id':'ob-1','status':'open','owner':'moxy','source_ref':'fixture:records'}
    ob.update({'kind':'buying_question','question':'Video of your pussy'} if kind=='buying_question' else {'product_id':'second'})
    update_fan(store,TENANT,ACCOUNT,101,state={'obligations':[ob]},actor='fixture-operator',evidence_ref='fixture:records')
    incoming(store,'I came here to see your pussy not talk about all this random stuff')
    identity,_=run(service,engine,decision('offer','Not that, but this one is close.','first',sequence='fixture_level',resolve_obligation='ob-1'))
    assert status(engine,identity)==expected
    store.close()


def test_em_and_en_dashes_are_stripped_from_fan_text():
    d={'action':'reply','text':'i know — but no – ok','product_id':None,'sequence':None,'flags':[],'reason':'x',
       'permission':None,'question':None,'resolve_obligation':None,'messages':[{'text':'i know — but no – ok','media':False}]}
    v=writer.parse(json.dumps(d))
    assert '—' not in v['text'] and '–' not in v['text'] and v['messages'][0]['text']==v['text']


def _corrections(engine):
    from sqlalchemy import select
    with Session(engine) as s:
        return [e for e in s.scalars(select(MoxyEvent)) if 'correction_of' in json.loads(e.payload_json)]


def _cases(engine):
    from sqlalchemy import select
    from db.onlyfans_case_models import OfOperatorCase
    with Session(engine) as s:
        return [c for c in s.scalars(select(OfOperatorCase)) if c.reason=='moxy_correction_failed']


def _blocked_first(engine,store,service):
    incoming(store,'would you suck me until I cum')
    first,_=run(service,engine,decision('offer','Here it is again.','missing-product',sequence='fixture_level'),payload={'source_message_id':2})
    return first


def test_recoverable_block_queues_exactly_one_bounded_correction(database,monkeypatch):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    first=_blocked_first(engine,store,service)
    with Session(engine) as s:e=s.get(MoxyEvent,(ACCOUNT,first));assert e.status=='held' and e.reason=='real_inventory_missing_or_stale'
    c=_corrections(engine);assert len(c)==1 and c[0].status=='pending'
    p=json.loads(c[0].payload_json)
    assert p['source_message_id']==2 and p['correction_of']==first
    assert p['rejected']=={'reason':'real_inventory_missing_or_stale','action':'offer','product_id':'missing-product'}
    assert not _cases(engine)
    store.close()


def test_correction_repeating_rejected_product_is_held_and_escalated_once(database,monkeypatch):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    first=_blocked_first(engine,store,service);c=_corrections(engine)[0]
    service.prepare(TENANT,ACCOUNT,c.event_id,model(decision('offer','Again.','missing-product',sequence='fixture_level')))
    with Session(engine) as s:e=s.get(MoxyEvent,(ACCOUNT,c.event_id));assert e.status=='held' and e.reason=='correction_repeated_rejected_choice'
    assert len(_corrections(engine))==1
    cases=_cases(engine);assert len(cases)==1 and cases[0].owner=='fixture-operator' and cases[0].category=='other'
    ev=json.loads(cases[0].evidence_json)
    assert ev['event_id']==c.event_id and ev['correction_of']==first
    assert ev['reason']=='correction_repeated_rejected_choice' and ev['rejected_reason']=='real_inventory_missing_or_stale'
    store.close()


def test_correction_may_choose_a_different_eligible_product(database,monkeypatch):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    _blocked_first(engine,store,service);c=_corrections(engine)[0]
    service.prepare(TENANT,ACCOUNT,c.event_id,model(decision('offer','This one is close.','first',sequence='fixture_level')))
    assert status(engine,c.event_id)=='ready' and not _cases(engine)
    store.close()


def test_correction_may_be_a_genuine_text_reply(database,monkeypatch):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    _blocked_first(engine,store,service);c=_corrections(engine)[0]
    m=model(decision('reply','ha, bold. tell me more about your day first'))
    service.prepare(TENANT,ACCOUNT,c.event_id,m)
    assert status(engine,c.event_id)=='ready' and not _cases(engine)
    assert 'event.payload.rejected' in json.dumps(m.calls[0]) and 'missing-product' in json.dumps(m.calls[0])
    store.close()


def test_non_recoverable_block_creates_no_correction(database,monkeypatch):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    incoming(store,'Do not offer me any more content.')
    first,_=run(service,engine,decision('offer','Buy this.','first',sequence='fixture_level',flags=['refusal'],
        observations=[evidence('sales_refused','Do not offer me any more content.')]),payload={'source_message_id':2})
    with Session(engine) as s:e=s.get(MoxyEvent,(ACCOUNT,first));assert e.status=='held' and e.reason=='sales_paused_by_current_input'
    assert not _corrections(engine) and not _cases(engine)
    store.close()


def test_correction_for_superseded_input_is_stale_and_opens_no_case(database,monkeypatch):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    _blocked_first(engine,store,service);c=_corrections(engine)[0]
    incoming(store,'newer',mid=3)
    service.prepare(TENANT,ACCOUNT,c.event_id,model(decision('reply','hey you')))
    with Session(engine) as s:e=s.get(MoxyEvent,(ACCOUNT,c.event_id));assert e.status=='stale' and e.reason=='moxy_input_superseded'
    assert not _cases(engine)
    store.close()


def test_correction_with_unusable_model_output_is_held_and_escalated(database,monkeypatch):
    from onlyfans_runtime.native_planning import SyntheticNativeModel
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    _blocked_first(engine,store,service)
    fix=_corrections(engine)[0]
    service.prepare(TENANT,ACCOUNT,fix.event_id,SyntheticNativeModel(raw_output='not json'))
    assert status(engine,fix.event_id)=='held' and len(_corrections(engine))==1 and len(_cases(engine))==1
    store.close()


def test_prompt_carries_commercial_state_with_sent_unpaid_card(database,monkeypatch):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    from db.models import Message
    from onlyfans_runtime.store import utcnow
    with Session(engine) as s,s.begin():
        s.add(Message(account_id=ACCOUNT,fan_id=101,message_id=7000,direction='out',body='locked',media_ids='[7101]',
            media_count=1,price_cents=7500,is_paid=False,created_at=utcnow()))
    identity,m=run(service,engine,decision())
    user=json.loads(m.calls[0][1]['content'])
    cs=user['commercial_state']
    assert cs['sent_unpaid_cards'][0]['message_id']==7000 and cs['sent_unpaid_cards'][0]['product_id']=='first'
    assert cs['last_offer']['paid'] is False and 'verified_purchases' not in cs
    assert 'commercial_state' in m.calls[0][0]['content']
    store.close()


def test_examples_give_both_creators_sales_flow_and_stay_compact():
    from moxy_replacement.examples import references
    from moxy_replacement.configuration import canonical
    for creator in ('amelia','cristina'):
        r=references(creator);kinds=[e['kind'] for e in r['examples']]
        assert kinds.count('positive')>=4 and len(canonical(r).encode())<4096
        assert all('message_id' not in str(e) for e in r['examples'])  # provenance stays in the file
        assert all('—' not in t and '–' not in t for e in r['examples'] for d,t in e['lines'] if d=='out')
    assert any('placeholder, not text' in t for e in references('amelia')['examples'] for d,t in e['lines'])
    assert 'failure' in [e['kind'] for e in references('amelia')['examples']]


def test_recent_emoji_is_not_reused_and_parts_stay_consistent():
    h=[{'direction':'out','text':'hey you 😏'},{'direction':'in','text':'hi'},{'direction':'out','text':'lol 😂'}]
    d={'text':'oh you 😏 bad 😈','messages':[{'text':'oh you 😏 bad 😈','media':False},{'text':'😂','media':True}]}
    out=writer.dedupe_emoji(d,h)
    assert out['text']=='oh you bad 😈' and out['messages'][0]['text']==out['text']
    assert out['messages'][1]['text']=='😂'  # a part is never emptied


def test_message_plan_is_the_source_of_truth_for_text():
    d={'action':'reply','text':'old summary','product_id':None,'sequence':None,'flags':[],'reason':'x',
       'permission':None,'question':None,'resolve_obligation':None,'messages':[{'text':'hey there','media':False},{'text':'how was it','media':False}]}
    v=writer.parse(json.dumps(d))
    assert v['text']=='hey there' and v['messages'][0]['text']=='hey there'


def test_unusable_model_output_gets_one_correction_not_silence(database,monkeypatch):
    from onlyfans_runtime.native_planning import SyntheticNativeModel
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    incoming(store,'would you suck me until I cum')
    identity=service.add_event(TENANT,ACCOUNT,101,'turn','fmt-1',{'source_message_id':2})
    service.prepare(TENANT,ACCOUNT,identity,SyntheticNativeModel(raw_output='not json'))
    assert status(engine,identity)=='held'
    fix=_corrections(engine);assert len(fix)==1 and json.loads(fix[0].payload_json)['rejected']['product_id'] is None
    service.prepare(TENANT,ACCOUNT,fix[0].event_id,model(decision('reply','ha, bold. not that one babe')))
    assert status(engine,fix[0].event_id)=='ready' and _cases(engine)==[]
    store.close()
