"""Real Moxy writer/state/delivery integration, isolated PostgreSQL fixtures."""
from copy import deepcopy
from datetime import timedelta, timezone
import json
import sys
from pathlib import Path
import pytest
from sqlalchemy import select,func,text
from sqlalchemy.orm import Session
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_onlyfans_foundation_pg import database,due,event
from test_onlyfans_native_planning_pg import ready
from test_onlyfans_text_delivery_pg import TENANT,ACCOUNT,unthrottle
from db.models import Message,CatalogItem,VaultItem,OfConversationState,ContentOffer,LadderQuote
from db.onlyfans_delivery_models import OfDeliveryPolicy,OfTextIntent
from db.onlyfans_moxy_models import MoxyEvent,MoxyFan,MoxyAccount
from onlyfans_runtime.native_planning import SyntheticNativeModel
from onlyfans_runtime.text_delivery import SyntheticTextTransport
from onlyfans_runtime.store import utcnow,Refused
from moxy_replacement.configuration import load,digest
from moxy_replacement.runtime import MoxyService,snapshot


def decision(action='reply',text='How did training go today?',product_id=None,**extra):
    return {'action':action,'text':text,'product_id':product_id,'sequence':None,'flags':[],
        'reason':'Fixture decision; not a model-quality result.','permission':None,'question':None,
        'resolve_obligation':None,**extra}


def model(d,**kw):return SyntheticNativeModel(raw_output=json.dumps(d),**kw)


def setup(database,monkeypatch,creator='amelia',mode='on',sequence=False):
    _,engine=database
    store,authority,claim,planner=ready(database,monkeypatch)
    with Session(engine) as s,s.begin():
        p=s.get(OfDeliveryPolicy,ACCOUNT);p.media_enabled=True
        products=[];bindings={}
        for i,key in enumerate(['first','second','third','fourth']):
            now=utcnow();mid=7101+i
            desc='Synthetic fixture photograph of a sports outfit, no live/custom claim.'
            v=VaultItem(account_id=ACCOUNT,media_id=mid,kind='photo',description=desc,describe_status='described',describe_generated_at=now,content_hash='fixture-'+key,
                created_at=now,updated_at=now);s.add(v)
            item=CatalogItem(account_id=ACCOUNT,kind='image_set',label='Synthetic '+key,description_for_ai=desc,
                media_ids=json.dumps([mid]),preview_media_ids='[]',price_cents=7500,pricing_mode='fixed',
                enabled=True,is_free_teaser=False,created_at=now,updated_at=now);s.add(item);s.flush()
            ref='asset:'+digest({'media_id':mid,'content_hash':v.content_hash,'description':desc,'updated_at':now.isoformat()})
            products.append({'id':key,'revision':1,'category':'bikini_lingerie_booty','price_cents':7500,'currency':'USD',
                'assets':[{'id':mid,'revision':1,'facts':[desc],'evidence_ref':ref}],
                'source':{'document':'01_Amelia.pdf' if creator=='amelia' else '03_Cristina.pdf','pages':[2]},
                'approval_ref':'fixture:approved-'+key,'available':True,'requires':{},'caption_instructions':'Accurate outfit photograph; no unsupported promise.'})
            bindings[key]=item.id
    cfg=load(creator)
    settings={'creator_key':creator,'products':products,'catalogue_bindings':bindings,
        'sequences':{'vault':['first','second','third','fourth']} if sequence else {'fixture_level':['first','second','third','fourth']},
        'sequence_levels':{'vault':[['first','second','third','fourth']], 'fixture_level':[['first','second','third','fourth']]},
        'operational':{field:'fixture-authority' for field in cfg.creator['required_operational_fields']},
        'public_facts':{'hobby':'gym; approved synthetic fixture fact'},'coordination_verified':True,'fixture':True,
        'approved_remedies':{'first':{'complimentary':True,'approval_ref':'fixture:recovery'}}}
    settings['operational']['assigned_operator']='fixture-operator'
    service=MoxyService(store)
    service.configure(TENANT,ACCOUNT,expected_revision=0,mode=mode,settings=settings,actor='fixture-operator')
    service.fan_facts(TENANT,ACCOUNT,101,{'ownership_verified':True,'purchase_links_verified':True,
        'vip':True,'spender':True,'high_spender':True},actor='fixture-operator',evidence_ref='fixture:fan-authority')
    return engine,store,service,authority,claim,settings


def status(engine,eventid):
    with Session(engine) as s:return s.get(MoxyEvent,(ACCOUNT,eventid)).status


def run(service,engine,d,*,kind='turn',key='fixture-1',payload=None):
    identity=service.add_event(TENANT,ACCOUNT,101,kind,key,payload or {})
    m=model(d);service.prepare(TENANT,ACCOUNT,identity,m)
    return identity,m


@pytest.mark.parametrize('creator',['amelia','cristina'])
def test_integrated_reply_writer_is_not_legacy_composer(database,monkeypatch,creator):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch,creator)
    identity,m=run(service,engine,decision())
    assert status(engine,identity)=='ready'
    prompt=json.dumps(m.calls[0])
    assert 'single' in prompt or 'decision owner' in prompt
    assert 'No message-count quota' in prompt and creator in prompt
    assert 'Never mention AI, bots, clones' not in prompt
    intent=service.queue(TENANT,ACCOUNT,identity)
    transport=SyntheticTextTransport(ACCOUNT)
    assert authority.dispatch(TENANT,ACCOUNT,intent,transport)=='accepted'
    assert status(engine,identity)=='done' and len(transport.calls)==1
    with Session(engine) as s:
        assert s.get(Message,(ACCOUNT,101,9001)).body=='How did training go today?'
    assert service.prepare(TENANT,ACCOUNT,identity,model(decision()))==identity
    store.close()


def test_contextual_offer_uses_same_text_and_actual_asset_price(database,monkeypatch):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    d=decision('offer','Here’s that outfit photo you asked about.', 'first',sequence='fixture_level')
    identity,m=run(service,engine,d)
    intent=service.queue(TENANT,ACCOUNT,identity)
    transport=SyntheticTextTransport(ACCOUNT)
    assert authority.dispatch(TENANT,ACCOUNT,intent,transport)=='accepted'
    assert transport.calls[0].media_ids==(7101,) and transport.calls[0].price_cents==7500
    with Session(engine) as s:
        assert s.get(Message,(ACCOUNT,101,9001)).is_paid is False
        assert s.scalar(select(ContentOffer)).status=='open'
        assert s.scalar(select(func.count()).select_from(LadderQuote))==0
    store.close()


def test_preview_generates_decision_but_never_sends_or_advances_state(database,monkeypatch):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch,mode='preview')
    identity,m=run(service,engine,decision('invite','Want to see the outfit photo?', 'first',sequence='fixture_level'))
    assert status(engine,identity)=='preview'
    with pytest.raises(Refused):service.queue(TENANT,ACCOUNT,identity)
    with Session(engine) as s:
        assert json.loads(s.get(MoxyFan,(ACCOUNT,101)).state_json)=={}
        assert s.scalar(select(func.count()).select_from(OfTextIntent))==0
    store.close()


@pytest.mark.parametrize('change',['input','takeover','asset','price','off'])
def test_generation_and_final_delivery_revalidate_current_state(database,monkeypatch,change):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    identity,m=run(service,engine,decision('offer','Here is the actual outfit photo.', 'first',sequence='fixture_level'))
    intent=service.queue(TENANT,ACCOUNT,identity)
    if change=='input':store.record(TENANT,ACCOUNT,event(mid=2,body='Actually, hold on.'));store.drain(TENANT,ACCOUNT)
    elif change=='off':service.configure(TENANT,ACCOUNT,expected_revision=1,mode='off',settings=settings,actor='fixture-operator')
    else:
        with Session(engine) as s,s.begin():
            if change=='takeover':s.get(OfConversationState,(ACCOUNT,101)).mode='human_controlled'
            if change=='asset':s.get(VaultItem,(ACCOUNT,7101)).removed_at=utcnow()
            if change=='price':s.get(CatalogItem,settings['catalogue_bindings']['first']).price_cents=8000
    transport=SyntheticTextTransport(ACCOUNT)
    assert authority.dispatch(TENANT,ACCOUNT,intent,transport) in {'cancelled','refused'}
    assert transport.calls==[]
    store.close()


def test_timeout_after_provider_acceptance_is_not_retried_on_restart(database,monkeypatch):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    identity,m=run(service,engine,decision())
    intent=service.queue(TENANT,ACCOUNT,identity)
    transport=SyntheticTextTransport(ACCOUNT,outcome='accepted_lost_response')
    assert authority.dispatch(TENANT,ACCOUNT,intent,transport)=='unknown'
    again=MoxyService(store)
    assert again.prepare(TENANT,ACCOUNT,identity,model(decision()))==identity
    assert authority.dispatch(TENANT,ACCOUNT,intent,transport)=='unknown' and len(transport.calls)==1
    store.close()


def test_new_input_while_model_runs_drops_stale_draft(database,monkeypatch):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    identity=service.add_event(TENANT,ACCOUNT,101,'turn','generation-arrival',{})
    m=model(decision(),on_generate=lambda:(store.record(TENANT,ACCOUNT,event(mid=2,body='A new question')),store.drain(TENANT,ACCOUNT)))
    service.prepare(TENANT,ACCOUNT,identity,m)
    assert status(engine,identity)=='stale'
    with pytest.raises(Refused):service.queue(TENANT,ACCOUNT,identity)
    store.close()


def test_no_silent_legacy_writer_or_queued_campaign_after_selection(database,monkeypatch):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    with pytest.raises(Refused,match='authoritative_writer'):
        authority.prepare(TENANT,ACCOUNT,101,command_key='old-automated',expected_revision=0,
            body='Old caption must not send',origin='automated',actor='fixture-old',work=claim)
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(MoxyEvent))==1
        assert s.scalar(select(func.count()).select_from(OfTextIntent))==0
    store.close()


def test_duplicate_event_keys_and_restart_keep_one_model_call(database,monkeypatch):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    identity,m=run(service,engine,decision())
    duplicate=service.add_event(TENANT,ACCOUNT,101,'turn','fixture-1',{})
    assert duplicate==identity
    later=model(decision());MoxyService(store).prepare(TENANT,ACCOUNT,identity,later)
    assert later.calls==[]
    store.close()


@pytest.mark.parametrize('state,fields,reason',[
    ('pending',{'describe_status':None},'media_description_pending'),
    ('failed',{'describe_status':'failed'},'media_description_failed'),
    ('unsupported',{'describe_status':'blocked_drm'},'media_description_unsupported'),
    ('content-changed',{'content_hash':'fixture-changed','ai_fields_json':'{"_content_stamp": ":fixture-first"}'},'media_description_pending'),
    # Unrelated metadata never makes the DESCRIPTION pending; the approved evidence
    # binding (which embeds updated_at) still fails closed until it is re-bound.
    ('metadata-only',{'updated_at':utcnow()+timedelta(days=1)},'asset_evidence_stale'),
])
def test_asset_description_state_excludes_product_with_visible_reason(database,monkeypatch,state,fields,reason):
    from moxy_replacement.runtime import account_readiness
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    with Session(engine) as s,s.begin():
        assert account_readiness(s,ACCOUNT,settings)['excluded_products']=={}
        v=s.get(VaultItem,(ACCOUNT,7101))
        for k,val in fields.items():setattr(v,k,val)
    with Session(engine) as s:
        r=account_readiness(s,ACCOUNT,settings)
    assert r['excluded_products']=={'first':reason+':7101'}
    assert not r['ready']
    if reason.startswith('media_description_'):assert 'product_excluded:first:%s:7101'%reason in r['blockers']
    store.close()


def test_removed_asset_is_missing_description_exclusion(database,monkeypatch):
    from moxy_replacement.runtime import account_readiness
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    with Session(engine) as s,s.begin():
        s.get(VaultItem,(ACCOUNT,7102)).removed_at=utcnow()
    with Session(engine) as s:
        r=account_readiness(s,ACCOUNT,settings)
    assert r['excluded_products']=={'second':'media_description_missing:7102'}
    store.close()


def _unlinked(s,fan_id,tx_id,amount=6900):
    """An unresolved paid PPV with no provider message identity, after the current binding."""
    from db.models import Transaction
    s.add(Transaction(id=tx_id,account_id=ACCOUNT,fan_id=fan_id,kind='ppv_message',message_id=None,amount_cents=amount,
        currency='USD',occurred_at=utcnow(),source='ledger',status='pending',provider_transaction_id='fixture-%d'%tx_id))


def test_other_fans_unlinked_purchases_do_not_freeze_this_fans_card_at_prepare_or_dispatch(database,monkeypatch):
    """Launch acceptance: 485092/485096-shaped rows for other known fans stay unresolved; fan 101 still gets a card."""
    from onlyfans_runtime.commerce import held
    from onlyfans_runtime import media_delivery
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    with Session(engine) as s,s.begin():
        _unlinked(s,15599495,485092);_unlinked(s,601923768,485096,1200)
    with Session(engine) as s:
        assert held(s,ACCOUNT),'the account view still shows the unresolved purchases'
        assert held(s,ACCOUNT,15599495) and held(s,ACCOUNT,601923768),'each buyer keeps its protection'
        assert not held(s,ACCOUNT,101)
        with pytest.raises(Refused,match='commerce evidence needs review'):
            media_delivery.assert_unowned(s,ACCOUNT,15599495,{'media_ids':[7101],'preview_ids':[]})
    d=decision('offer','Here’s that outfit photo you asked about.','first',sequence='fixture_level')
    identity,m=run(service,engine,d)
    assert status(engine,identity)!='held'
    intent=service.queue(TENANT,ACCOUNT,identity)
    # A new unlinked sale for another known fan lands between queueing and dispatch.
    with Session(engine) as s,s.begin():_unlinked(s,777001,485200,1500)
    transport=SyntheticTextTransport(ACCOUNT)
    assert authority.dispatch(TENANT,ACCOUNT,intent,transport)=='accepted'
    assert transport.calls[0].media_ids==(7101,) and transport.calls[0].price_cents==7500
    from db.models import Transaction
    with Session(engine) as s:
        rows=s.scalars(select(Transaction).order_by(Transaction.id)).all()
        assert [(r.id,r.fan_id,r.message_id,r.status) for r in rows]==[
            (485092,15599495,None,'pending'),(485096,601923768,None,'pending'),(485200,777001,None,'pending')]
    store.close()


def test_unknown_buyer_unlinked_purchase_still_holds_this_fans_card(database,monkeypatch):
    from onlyfans_runtime.commerce import held
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    with Session(engine) as s,s.begin():_unlinked(s,None,485300)
    with Session(engine) as s:assert held(s,ACCOUNT,101)
    d=decision('offer','Here’s that outfit photo you asked about.','first',sequence='fixture_level')
    identity,m=run(service,engine,d)
    with Session(engine) as s:
        e=s.get(MoxyEvent,(ACCOUNT,identity))
        assert e.status!='done' or s.scalar(select(func.count()).select_from(OfTextIntent).where(OfTextIntent.catalog_item_id.is_not(None)))==0
    store.close()
