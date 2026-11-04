"""Ground the documented personal-routine failure without adding biography."""
import json
import pytest
from sqlalchemy.orm import Session
from sqlalchemy import select
from test_onlyfans_foundation_pg import database, event
from test_moxy_integrated_pg import setup, model, decision
from test_onlyfans_text_delivery_pg import TENANT, ACCOUNT
from db.onlyfans_moxy_models import MoxyEvent
from moxy_replacement.grounding import unsupported_routine


@pytest.mark.parametrize('text',[
    "mornings are usually workouts, then i'm here with you lot the rest of the day",
    "I coach a few sessions and film a bit on the side",
    "i bounce between the gym and this",
    "I'm a trainer",
    "I work as a coach",
    "I keep my day job private",
    "I keep my work routine private, but I am happy to chat here",
    "My work life is private",
    "nah, this is it for me",
    "no secret day job hiding behind the page lol",
    "honestly i don't keep a set time, it just depends on the day",
    "no set schedule on my end",
    "i just go when i can, some weeks mornings, some weeks after everything else",

])
def test_unverified_routine_is_refused_even_if_an_old_reply_claimed_it(text):
    assert unsupported_routine({'public_facts':{},'history':[{'direction':'in','text':'What do you do for work and when do you work out?'},{'direction':'out','text':text}]},[text])


@pytest.mark.parametrize('text',[
    "I keep my location private.",
    "My family stays private.",
    "What do your mornings usually look like?",
    "I don't coach or train anyone.",
    "Here is the outfit photo you asked about.",
])
def test_privacy_questions_and_commerce_are_unchanged(text):
    assert not unsupported_routine({'public_facts':{}},[text])


def test_only_an_exact_approved_public_fact_grounds_the_routine():
    line='I usually train in the mornings'
    assert not unsupported_routine({'public_facts':{'approved':line}},[line])
    assert unsupported_routine({'public_facts':{'hobby':'gym'}},[line])
    assert unsupported_routine({'public_facts':{'approved':line}},[line+' and coach clients after'])


def test_actual_service_exhausted_routine_has_no_invented_privacy_fallback(database,monkeypatch):
    engine,store,service,_,_,_=setup(database,monkeypatch)
    store.record(TENANT,ACCOUNT,event(mid=2,body='What do you do for work?'));store.drain(TENANT,ACCOUNT)
    identity=service.add_event(TENANT,ACCOUNT,101,'turn','routine-repair',{'source_message_id':2})
    service.prepare(TENANT,ACCOUNT,identity,model(decision(text='I coach a few sessions every morning')))
    with Session(engine) as s:
        first=s.get(MoxyEvent,(ACCOUNT,identity));assert first.reason=='unsupported_creator_routine'
        retry=next(e for e in s.scalars(select(MoxyEvent)) if json.loads(e.payload_json).get('correction_of'))
        retry_id=retry.event_id
        brief=json.loads(retry.payload_json)['rejected']
        assert brief['kind']=='factual' and brief['public_facts']=={'hobby':'gym; approved synthetic fixture fact'}
    service.prepare(TENANT,ACCOUNT,retry_id,model(decision(text='I usually train every morning')))
    with Session(engine) as s:
        final=s.get(MoxyEvent,(ACCOUNT,retry_id))
        assert final.status=='held' and final.reason=='unsupported_creator_routine'
        assert json.loads(final.decision_json)['text']=='I usually train every morning'
        assert final.intent_id is None
        from db.onlyfans_case_models import OfOperatorCase
        case=s.scalar(select(OfOperatorCase).where(OfOperatorCase.account_id==ACCOUNT))
        assert case.scope=='offer'
    store.close()



def test_gaze_wording_correction_preserves_exact_valid_offer(database,monkeypatch):
    engine,store,service,authority,_,_=setup(database,monkeypatch)
    identity=service.add_event(TENANT,ACCOUNT,101,'turn','gaze-repair',{'source_message_id':1})
    service.prepare(TENANT,ACCOUNT,identity,model(decision('offer','looking right back at you','first',sequence='fixture_level')))
    with Session(engine) as session:
        assert session.get(MoxyEvent,(ACCOUNT,identity)).reason=='unsupported_asset_gaze'
        retry=next(e for e in session.scalars(select(MoxyEvent)) if json.loads(e.payload_json).get('correction_of'))
        retry_id=retry.event_id;brief=json.loads(retry.payload_json)['rejected']
        assert brief['kind']=='factual' and brief['keep']=={'action':'offer','product_id':'first','sequence':'fixture_level'}
        assert brief['keep_terms']=={'price_cents':7500,'media_ids':[7101],'preview_ids':[]}
    service.prepare(TENANT,ACCOUNT,retry_id,model(decision('offer','Here is that sports outfit photo.','first',sequence='fixture_level')))
    intent=service.queue(TENANT,ACCOUNT,retry_id)
    from onlyfans_runtime.text_delivery import SyntheticTextTransport
    wire=SyntheticTextTransport(ACCOUNT)
    assert authority.dispatch(TENANT,ACCOUNT,intent,wire)=='accepted'
    assert len(wire.calls)==1 and wire.calls[0].media_ids==(7101,) and wire.calls[0].price_cents==7500
    store.close()


def test_specific_invitation_must_bind_product_before_acceptance(database,monkeypatch):
    engine,store,service,authority,_,_=setup(database,monkeypatch)
    identity=service.add_event(TENANT,ACCOUNT,101,'turn','invitation-identity',{'source_message_id':1})
    service.prepare(TENANT,ACCOUNT,identity,model(decision(text='Want me to send the outfit photo?',reason='Invite product first.')))
    with Session(engine) as session:
        assert session.get(MoxyEvent,(ACCOUNT,identity)).reason=='product_invitation_missing_identity'
        retry=next(e for e in session.scalars(select(MoxyEvent)) if json.loads(e.payload_json).get('correction_of'))
        retry_id=retry.event_id
        assert json.loads(retry.payload_json)['rejected']['kind']=='product'
    service.prepare(TENANT,ACCOUNT,retry_id,model(decision('invite','Would you like the sports outfit photo?','first',sequence='fixture_level')))
    intent=service.queue(TENANT,ACCOUNT,retry_id)
    from onlyfans_runtime.text_delivery import SyntheticTextTransport
    from db.onlyfans_moxy_models import MoxyFan
    wire=SyntheticTextTransport(ACCOUNT)
    assert authority.dispatch(TENANT,ACCOUNT,intent,wire)=='accepted'
    assert wire.calls[0].media_ids==()
    with Session(engine) as session:
        assert json.loads(session.get(MoxyFan,(ACCOUNT,101)).state_json)['invitation']['product_id']=='first'
    store.close()


def test_verified_asset_gaze_and_noncommercial_flirting_are_not_blocked():
    from moxy_replacement.grounding import unsupported_asset_gaze,unbound_product_invitation
    text='looking right back at you'
    snap={'products':{'real-item':{'assets':[{'facts':['She is looking directly at the camera.']}]}}}
    assert not unsupported_asset_gaze(snap,{'product_id':'real-item'},[text])
    assert not unsupported_asset_gaze({'products':{}},{'product_id':None},[text])
    assert not unbound_product_invitation(snap,{'action':'reply','product_id':None,'reason':'ordinary chat'},['Want to see what happens next?'])



def test_declarative_product_proposal_is_bound_but_existing_card_question_is_allowed():
    from moxy_replacement.grounding import unbound_product_invitation
    key='moxy-specific-real-key'
    snap={'products':{key:{'assets':[{'facts':['Black thong, sheer floral garment pulled down to thighs, legs spread.']}]}}}
    decision={'action':'reply','product_id':None,'reason':'Describing '+key}
    texts=['black thong, sheer floral garment at my thighs, legs spread. yours when you say the word']
    assert unbound_product_invitation(snap,decision,texts)
    snap['card_state']=[{'product_id':key}]
    assert not unbound_product_invitation(snap,decision,texts)



@pytest.mark.parametrize('change',[None,'canonical_memory','historical_excerpt','dropped_source'])
def test_own_append_can_move_a_source_into_historical_memory(database,monkeypatch,change):
    from datetime import timedelta
    from db.models import Message, Fan
    from onlyfans_runtime.store import utcnow
    from onlyfans_runtime.text_delivery import SyntheticTextTransport
    from test_onlyfans_text_delivery_pg import unthrottle
    engine,store,service,authority,_,_=setup(database,monkeypatch)
    now=utcnow()
    with Session(engine) as session,session.begin():
        session.get(Message,(ACCOUNT,101,1)).created_at=now-timedelta(days=2)
        for i in range(7):
            session.add(Message(account_id=ACCOUNT,fan_id=101,message_id=300+i,direction='out',
                body='Work was busy in historical source %d.'%i,
                created_at=now-timedelta(days=3)+timedelta(seconds=i)))
        for i in range(100):
            session.add(Message(account_id=ACCOUNT,fan_id=101,message_id=100+i,direction='out',
                body='Work ran late on that old Tuesday.' if i==1 else 'old ordinary chat %d'%i,
                created_at=now-timedelta(days=1)+timedelta(seconds=i)))
    store.record(TENANT,ACCOUNT,event(mid=2,body='How did work go?'));store.drain(TENANT,ACCOUNT)
    identity=service.add_event(TENANT,ACCOUNT,101,'turn','memory-window',{'source_message_id':2})
    service.prepare(TENANT,ACCOUNT,identity,model(decision(text='That sounds like a busy one.',
        messages=[{'text':'That sounds like a busy one.','media':False},{'text':'What happened next?','media':False}])))
    with Session(engine) as session:
        before=json.loads(session.get(MoxyEvent,(ACCOUNT,identity)).snapshot_json)
        assert before['history'][0]['id']==101 and len(before['memory']['interactions'])==6
        dropped_id=int(before['memory']['interactions'][-1]['sourceMessageId'])
    first=service.queue(TENANT,ACCOUNT,identity)
    def external_change(_dispatch):
        if change is None:return
        with Session(engine) as session,session.begin():
            if change=='canonical_memory':session.get(Fan,(ACCOUNT,101)).real_name='Different Verified Name'
            else:
                changed=101 if change=='historical_excerpt' else dropped_id
                session.get(Message,(ACCOUNT,101,changed)).body='Independently edited historical source'
    assert authority.dispatch(TENANT,ACCOUNT,first,SyntheticTextTransport(ACCOUNT,message_id=9001,on_send=external_change))=='accepted'
    with Session(engine) as session:
        current=session.get(MoxyEvent,(ACCOUNT,identity))
        if change is not None:
            assert current.status=='stale' and current.reason=='moxy_conversation_interrupted'
            store.close()
            return
        assert current.status=='ready'
        after=json.loads(current.snapshot_json)
        assert after['memory']['interactions'][0]['sourceMessageId']=='101'
    unthrottle(engine);second=service.queue(TENANT,ACCOUNT,identity)
    assert authority.dispatch(TENANT,ACCOUNT,second,SyntheticTextTransport(ACCOUNT,message_id=9002))=='accepted'
    with Session(engine) as session:assert session.get(MoxyEvent,(ACCOUNT,identity)).status=='done'
    store.close()



def test_exact_verified_purchase_is_acknowledged_without_promoting_a_fan_claim():
    from moxy_replacement.grounding import ignores_verified_ownership
    s={'commercial_state':{'last_offer':{'product_id':'p','paid':True},'verified_purchases':['p'],
        'current_input':{'text':'I paid for it. Do I already own that exact one?'}}}
    text=['Let me check my side before I say anything for sure']
    assert ignores_verified_ownership(s,text)
    assert not ignores_verified_ownership(s,['Yes, that one is already yours.'])
    s['commercial_state']['verified_purchases']=[]
    assert not ignores_verified_ownership(s,text)
    s['commercial_state']['verified_purchases']=['p']
    s['commercial_state']['current_input']['text']='Do I own the earlier video?'
    assert not ignores_verified_ownership(s,text)


def test_negative_gaze_fact_does_not_authorise_a_positive_caption():
    from moxy_replacement.grounding import unsupported_asset_gaze
    d={'product_id':'p'}
    s={'products':{'p':{'assets':[{'facts':['She is looking away from the camera.']}]}}}
    assert unsupported_asset_gaze(s,d,['looking right back at you'])


def test_approved_fact_or_explicit_privacy_preference_is_accepted_without_biography_inference():
    line="I keep my work routine private"
    assert not unsupported_routine({'public_facts':{'approved_privacy':line}},[line])
    assert unsupported_routine({'public_facts':{},'internal_biography':{'job':'Content creator, Gym'}},['I work as a coach'])


@pytest.mark.parametrize('text',["I keep where I train private.","Where I work stays private.","I keep my gym address private."])
def test_real_identifying_location_boundaries_are_preserved(text):
    assert not unsupported_routine({'public_facts':{},'history':[{'direction':'in','text':'Where is your gym?'}]},[text])


def test_general_chat_is_not_treated_as_a_creator_job_or_routine_claim():
    snap={'public_facts':{},'history':[{'direction':'in','text':'Which outfit do you like?'}]}
    assert not unsupported_routine(snap,['It depends on the day.'])
    assert not unsupported_routine(snap,['This is it for me.'])


def test_genuine_location_boundary_does_not_authorise_an_appended_routine():
    assert unsupported_routine({'public_facts':{}},['I keep where I train private and I coach clients every morning'])
