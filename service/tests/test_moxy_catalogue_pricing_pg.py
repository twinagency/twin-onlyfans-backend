"""Source prices -> real catalogue store -> Moxy authority/delivery, synthetic only."""
import json
from copy import deepcopy
import pytest
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_onlyfans_offer_import_pg import ready,refresh,raw,observation
from test_onlyfans_connections_pg import TENANT,ACTOR
from db.models import VaultItem,CatalogItem
from db.onlyfans_offer_import_models import OfOfferImportDecision
from db.onlyfans_vault_models import OfVaultProductCreate
from moxy_replacement.configuration import load,ActionBlocked
from moxy_replacement.authority import ProductAuthority,readiness
from moxy_replacement.pricing import validate_price,validate_format,asset_evidence
from onlyfans_runtime.offer_import import moxy_candidates,materialize_moxy_products
from onlyfans_runtime.store import utcnow


@pytest.fixture(autouse=True)
def fixture_creator_binding(monkeypatch):
    monkeypatch.setattr('onlyfans_runtime.offer_import._moxy_source_bindings',
        lambda:{'of-7001':'amelia'})

def classified(ready, *, category='feet_photos', price=1999, previews=None):
    store,account=ready
    with Session(store.engine) as s,s.begin():
        for mid in [11,12]:
            v=s.get(VaultItem,(account,mid));v.description='Synthetic photograph of bare feet on a blue mat.'
            v.describe_status='described';v.content_hash='feet-fixture-'+str(mid)
    refresh(store,account,[(501,1001)],{(501,None):([raw(1001,[11,12],price,
        previews=[] if previews is None else previews,locked=False)],False)})
    c=next(c for c in moxy_candidates(store,TENANT,account,creator_key='amelia')['items'] if c['ready'])
    value={k:c[k] for k in ['observation_id','fingerprint','configuration_fingerprint']}
    value.update(category=category,asset_evidence={str(a['id']):a['evidence_ref'] for a in c['assets']},
        category_quotes=[{'media_id':a['id'],'quote':'bare feet'} for a in c['assets']],
        reason='Both described photographs show feet; fixed source photo price.')
    return store,account,c,value

def build(store,account,value,creator='amelia'):
    return materialize_moxy_products(store,TENANT,account,creator_key=creator,
        classifications=[value],actor=ACTOR)[0]

@pytest.mark.parametrize('creator,category,price',[
    ('amelia','feet_photos',2000),('amelia','nude_booty',7500),
    ('amelia','covered_booty',3000),('cristina','booty_photos',6000)])
def test_source_fixed_price_is_not_a_floor(creator,category,price):
    p=load(creator).resolve('product:'+category,{}).value
    assert p['exact_cents']==price and 'minimum_cents' not in p
    validate_price(p,price)
    with pytest.raises(ActionBlocked):validate_price(p,price+100)

def test_minimum_accepts_verified_price_but_not_lower():
    p=load('cristina').resolve('product:bikini_lingerie_booty',{}).value
    assert p['minimum_cents']==6000 and 'exact_cents' not in p
    validate_price(p,7000)
    with pytest.raises(ActionBlocked):validate_price(p,5999)

def test_fixed_source_price_materializes_exact_previews_without_manual_approvals(ready):
    store,account,c,v=classified(ready,previews=[11])
    r=build(store,account,v)
    assert r['ok'] and r['priceCents']==2000 and r['enabled'] is False
    assert r['evidence']['preview_ids']==[11] and r['evidence']['media_ids']==[11,12]
    assert r['product']['approval_ref'].startswith('derived-catalogue:')
    again=build(store,account,v)
    assert again['itemId']==r['itemId']
    with Session(store.engine) as s:
        assert s.scalar(select(func.count()).select_from(CatalogItem))==1
        assert s.scalar(select(func.count()).select_from(OfVaultProductCreate))==1
        assert s.scalar(select(func.count()).select_from(OfOfferImportDecision))==0
        assert s.get(CatalogItem,r['itemId']).pricing_mode=='fixed'

def test_one_bad_offer_does_not_block_independent_good_offer(ready):
    store,account,c,v=classified(ready)
    bad={**v,'asset_evidence':{'11':'wrong'}}
    rs=materialize_moxy_products(store,TENANT,account,creator_key='amelia',
        classifications=[bad,v],actor=ACTOR)
    assert rs[0]['ok'] is False and rs[1]['ok'] is True

@pytest.mark.parametrize('change',['description','kind','removed','source'])
def test_changed_evidence_cannot_create_or_rebind(ready,change):
    store,account,c,v=classified(ready)
    if change=='source':v['fingerprint']='d'*64
    with Session(store.engine) as s,s.begin():
        if change!='source':
            row=s.get(VaultItem,(account,11))
            if change=='description':row.description='Corrected: shoes only'
            elif change=='kind':row.kind='video'
            else:row.removed_at=utcnow()
    assert build(store,account,v)['ok'] is False

def test_unchanged_metadata_refresh_keeps_binding_stable(ready):
    store,account,c,v=classified(ready)
    first=build(store,account,v)
    with Session(store.engine) as s,s.begin():
        row=s.get(VaultItem,(account,11));before=asset_evidence(row)
        row.updated_at=utcnow();row.source_url='https://example.invalid/refreshed'
        assert asset_evidence(row)==before
    assert build(store,account,v)['itemId']==first['itemId']

def test_operator_price_correction_is_preserved(ready):
    store,account,c,v=classified(ready)
    first=build(store,account,v)
    with Session(store.engine) as s,s.begin():s.get(CatalogItem,first['itemId']).price_cents=2100
    assert build(store,account,v)['reason']=='operator_catalogue_correction_preserved'
    with Session(store.engine) as s:assert s.get(CatalogItem,first['itemId']).price_cents==2100

def test_ungrounded_category_quote_refused(ready):
    store,account,c,v=classified(ready)
    v['category_quotes'][0]['quote']='not present in the description'
    assert build(store,account,v)['reason']=='ungrounded_category_evidence'

def test_mixed_feet_format_requires_photo_and_video(ready):
    store,account,c,v=classified(ready,category='feet_photos_video')
    assert build(store,account,v)['reason']=='product_media_format_mismatch'

def test_cristina_quantity_excludes_free_preview():
    from types import SimpleNamespace
    rule=load('cristina').resolve('product:booty_photos',{}).value
    for n in [1,3,6]:
        with pytest.raises(ActionBlocked):validate_format(rule,[SimpleNamespace(kind='photo')]*n)
    for n in [4,5]:validate_format(rule,[SimpleNamespace(kind='photo')]*n)
    with pytest.raises(ActionBlocked):validate_format(rule,[SimpleNamespace(kind='video')]*4)

def test_no_sequence_requirement_removed_for_full_release():
    cfg=load('amelia');ops={k:'fixture' for k in cfg.creator['required_operational_fields']}
    assert 'authorised_vault_sequence_missing' in readiness(cfg,account_bound=True,products=[],
        sequences={},operational=ops,coordination_verified=True)['blockers']

def test_only_explicit_pilot_can_defer_unrelated_sequence_requirement():
    cfg=load('amelia');ops={k:'fixture' for k in cfg.creator['required_operational_fields']}
    ops.pop('vault_sequences')
    r=readiness(cfg,account_bound=True,products=[],sequences={},operational=ops,
        coordination_verified=True,pilot={'fan_ids':[101]})
    assert 'authorised_product_asset_mapping_missing' in r['blockers']
    assert 'authorised_vault_sequence_missing' not in r['blockers']
    assert 'operational_field_missing:vault_sequences' not in r['blockers']

def test_single_vault_asset_uses_fixed_document_price_without_historical_bundle(ready):
    from onlyfans_runtime.offer_import import moxy_vault_candidates
    store,account,c,v=classified(ready)
    single=next(c for c in moxy_vault_candidates(store,TENANT,account,creator_key='amelia')['items']
        if c['ready'] and c['vault_media_id']==11)
    v.update(observation_id=None,vault_media_id=11,fingerprint=single['fingerprint'],
        asset_evidence={'11':single['assets'][0]['evidence_ref']},
        category_quotes=[{'media_id':11,'quote':'bare feet'}])
    r=build(store,account,v)
    assert r['ok'] and r['priceCents']==2000 and r['evidence']['media_ids']==[11]
    assert r['evidence']['preview_ids']==[]
    with Session(store.engine) as s:
        assert s.scalar(select(func.count()).select_from(OfOfferImportDecision))==0

def test_single_vault_asset_cannot_turn_minimum_into_an_exact_price(ready):
    from onlyfans_runtime.offer_import import moxy_vault_candidates
    store,account,c,v=classified(ready)
    single=next(c for c in moxy_vault_candidates(store,TENANT,account,creator_key='amelia')['items']
        if c['ready'] and c['vault_media_id']==11)
    v.update(observation_id=None,vault_media_id=11,fingerprint=single['fingerprint'],
        category='covered_boobs',asset_evidence={'11':single['assets'][0]['evidence_ref']},
        category_quotes=[{'media_id':11,'quote':'bare feet'}])
    assert build(store,account,v)['reason']=='current_price_unresolved'

@pytest.mark.parametrize('refresh_metadata',[False,True])
def test_materialized_offer_reaches_existing_moxy_delivery_path(ready,refresh_metadata):
    from db.models import AccountAiConfig,OfConversationState
    from db.onlyfans_models import OfRuntime
    from db.onlyfans_delivery_models import OfDeliveryPolicy
    from moxy_replacement.runtime import MoxyService
    from onlyfans_runtime.text_delivery import TextAuthority,SyntheticTextTransport
    from test_onlyfans_foundation_pg import event
    from test_moxy_integrated_pg import decision,model
    store,account,c,v=classified(ready,previews=[11])
    made=build(store,account,v)
    product=made['product'];pid=product['id']
    TextAuthority(store).configure(TENANT,account,expected_revision=0,enabled=True,limit_units=20,throttle_seconds=1,actor='fixture-operator',reason='Synthetic source-to-delivery test')
    with Session(store.engine) as s,s.begin():
        rt=s.get(OfRuntime,account);rt.enabled=True;rt.recovery_needed=False;rt.last_sync_at=utcnow()
        policy=s.get(OfDeliveryPolicy,account);policy.enabled=True;policy.media_enabled=True
        policy.transport_kind='synthetic'
        cfg=s.get(AccountAiConfig,account)
        if cfg is None:
            cfg=AccountAiConfig(account_id=account);s.add(cfg)
        cfg.model='grok-4-1-fast-non-reasoning'
        cfg.daily_cost_cap_cents=100
        s.get(CatalogItem,made['itemId']).enabled=True
    store.record(TENANT,account,event(body='Can I buy those feet photos?'));store.drain(TENANT,account)
    with Session(store.engine) as s,s.begin():
        s.get(OfConversationState,(account,101)).mode='automated'
    ops={k:'fixture-authority' for k in load('amelia').creator['required_operational_fields']}
    ops.pop('vault_sequences')
    ops['assigned_operator']='fixture-operator'
    settings={'creator_key':'amelia','products':[product],'catalogue_bindings':made['catalogueBinding'],
        'sequences':{},'operational':ops,'coordination_verified':True,'fixture':True,
        'pilot':{'fan_ids':[101],'product_ids':[pid],'supervisor':'fixture-operator'}}
    service=MoxyService(store)
    assert service.configure(TENANT,account,expected_revision=0,mode='on',
        settings=settings,actor='fixture-operator')['readiness']['ready']
    service.fan_facts(TENANT,account,101,{'ownership_verified':True,'purchase_links_verified':True},
        actor='fixture-operator',evidence_ref='fixture:exact-ledger')
    identity=service.add_event(TENANT,account,101,'turn','source-to-delivery',{'source_message_id':1})
    generator=model(decision('offer','Here are the feet photographs you asked about.',pid))
    service.prepare(TENANT,account,identity,generator)
    if refresh_metadata:
        with Session(store.engine) as s,s.begin():
            row=s.get(VaultItem,(account,11));row.updated_at=utcnow()
            row.source_url='https://example.invalid/renewed-url'
    intent=service.queue(TENANT,account,identity)
    wire=SyntheticTextTransport(account)
    assert TextAuthority(store).dispatch(TENANT,account,intent,wire)=='accepted'
    assert wire.calls[0].price_cents==2000 and wire.calls[0].media_ids==(11,12)
    assert wire.calls[0].preview_ids==(11,)

def test_wrong_creator_sheet_cannot_price_another_account(ready):
    store,account,c,v=classified(ready)
    assert build(store,account,v,creator='cristina')['reason']=='creator_source_binding_mismatch'

def test_missing_creator_binding_cannot_create_products(ready,monkeypatch):
    store,account,c,v=classified(ready)
    monkeypatch.setattr('onlyfans_runtime.offer_import._moxy_source_bindings',lambda:{})
    assert build(store,account,v)['reason']=='creator_source_binding_missing'


def test_bounded_classifier_revalidates_after_model_call(ready):
    import asyncio
    from onlyfans_runtime.offer_import import prepare_moxy_products
    store,account,c,v=classified(ready)
    async def generate(messages):
        assert store.engine.pool.checkedout()==0
        with Session(store.engine) as s,s.begin():
            s.get(VaultItem,(account,11)).description='Operator corrected the asset.'
        return json.dumps({k:v[k] for k in ['category','reason','category_quotes']})
    result=asyncio.run(prepare_moxy_products(store,TENANT,account,creator_key='amelia',
        candidates=[c],generate=generate,actor=ACTOR))
    assert result[0]['reason']=='asset_classification_stale'

def test_classifier_cross_account_input_makes_no_call(ready):
    import asyncio
    from onlyfans_runtime.offer_import import prepare_moxy_products
    store,account,c,v=classified(ready)
    called=[]
    async def generate(messages):called.append(messages);raise AssertionError('must not call')
    result=asyncio.run(prepare_moxy_products(store,TENANT,account,creator_key='cristina',
        candidates=[c],generate=generate,actor=ACTOR))
    assert not called and result[0]['reason']=='candidate_account_scope_mismatch'

def test_matching_existing_fixed_item_is_reused_without_overwrite(ready):
    store,account,c,v=classified(ready)
    with Session(store.engine) as s,s.begin():
        item=CatalogItem(account_id=account,kind='image_set',label='Operator label',
            description_for_ai='Operator note',media_ids='[11,12]',preview_media_ids='[]',
            price_cents=2000,pricing_mode='fixed',enabled=False,is_free_teaser=False,
            created_at=utcnow(),updated_at=utcnow())
        s.add(item);s.flush();item_id=item.id
    result=build(store,account,v)
    assert result['ok'] and result['itemId']==item_id
    with Session(store.engine) as s:
        assert s.get(CatalogItem,item_id).label=='Operator label'
        assert s.scalar(select(func.count()).select_from(CatalogItem))==1

def test_target_history_refresh_never_visits_another_fan(ready):
    from native_pilot.tools.qualify_moxy_pilot_history import refresh_test_history
    from test_onlyfans_offer_import_pg import FakeHistory
    from db.models import Fan,OfConversationState
    store,account=ready
    with Session(store.engine) as s,s.begin():
        s.add(Fan(account_id=account,fan_id=505));s.flush()
        s.add(OfConversationState(account_id=account,fan_id=505,mode='paused',
            takeover_state='none',revision=0,input_revision=0))
    reader=FakeHistory({(505,None):([raw(5000,[11],2000,previews=[],locked=False,fan=505)],False)})
    reader.account_id=account
    result=refresh_test_history(store,TENANT,account,505,reader,connection_revision=1)
    assert result['complete'] and reader.calls==[(505,None)]
    with Session(store.engine) as s:
        assert s.get(OfConversationState,(account,505)).mode=='paused'

def test_target_history_refresh_refuses_an_unpaused_fan(ready):
    from native_pilot.tools.qualify_moxy_pilot_history import refresh_test_history
    from onlyfans_runtime.store import Refused
    from types import SimpleNamespace
    store,account=ready
    with pytest.raises(Refused):
        refresh_test_history(store,TENANT,account,501,SimpleNamespace(account_id=account),connection_revision=1)


def test_changed_creator_rules_invalidate_classification(ready):
    store,account,c,v=classified(ready)
    v['configuration_fingerprint']='f'*64
    assert build(store,account,v)['reason']=='creator_classification_stale'

def test_classifier_receives_creator_prohibitions(ready):
    from onlyfans_runtime.offer_import import moxy_classification_messages
    store,account,c,v=classified(ready)
    messages=moxy_classification_messages(c)
    assert 'kitty' in messages[0]['content'] and 'bootyhole' in messages[0]['content']
