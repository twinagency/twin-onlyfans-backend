"""Large catalogues keep every option and retain delivery verification."""
from copy import deepcopy
import json
import pytest
from sqlalchemy.orm import Session
from db.models import CatalogItem,VaultItem
from db.onlyfans_moxy_models import MoxyEvent
from onlyfans_runtime.store import utcnow
from onlyfans_runtime.text_delivery import SyntheticTextTransport
from moxy_replacement.configuration import canonical
from moxy_replacement.catalogue_context import project
from moxy_replacement.pricing import asset_evidence
from test_moxy_integrated_pg import setup,run,decision,status
from test_onlyfans_foundation_pg import database
from test_onlyfans_text_delivery_pg import TENANT,ACCOUNT

def test_projection_preserves_every_asset_and_condition():
    products={};inventory={}
    for i in range(100):
        key='product-%03d'%i
        products[key]={'category':'category','price_cents':3000,'currency':'USD','available':True,
            'requires':{'vip':i%2==0},'source':{'document':'01_Amelia.pdf','pages':[2]},
            'caption_instructions':'Use actual asset facts.',
            'assets':[{'id':i*2+1,'facts':['photo fact '+str(i)]},
                      {'id':i*2+2,'facts':['preview fact '+str(i)]}]}
        inventory[key]={'media_ids':[i*2+1,i*2+2],'preview_ids':[i*2+2],
            'item':{'id':i,'label':key},'files':[
                {'media_id':i*2+1,'kind':'photo','description_basis':{'frames':1}},
                {'media_id':i*2+2,'kind':'photo','description_basis':{'frames':1}}]}
    original={'products':products,'inventory':inventory,'instructions':{},
        'fan_state':{'owned_assets':[19],'previously_sent_assets':[21],'budget_cents':5000},
        'history':[{'direction':'in','text':'Which one?'}],'invitation':{'product_id':'product-099'},
        'sequences':{'s':['product-001','product-099']}}
    before=deepcopy(original);out=project(original)
    assert original==before and len(out['products'])==100
    assert out['fan_state']==original['fan_state'] and out['history']==original['history']
    assert out['invitation']==original['invitation'] and out['sequences']==original['sequences']
    for key,p in products.items():
        q=out['products'][key]
        assert q['requires']==p['requires'] and q['price_cents']==p['price_cents']
        assert q['media_ids']==inventory[key]['media_ids']
        assert q['preview_ids']==inventory[key]['preview_ids']
        assert [a['facts'] for a in q['assets']]==[a['facts'] for a in p['assets']]
    assert out['catalogue_caption_instructions']=='Use actual asset facts.'
    products['product-099']['caption_instructions']='A distinct operator instruction.'
    assert project(original)['products']['product-099']['caption_instructions']=='A distinct operator instruction.'

def test_hundred_products_reach_model_and_last_item_can_deliver(database,monkeypatch):
    engine,store,svc,authority,claim,settings=setup(database,monkeypatch)
    products=[];bindings={}
    with Session(engine) as s,s.begin():
        for i in range(100):
            key='large-%03d'%i;mid=18000+i;now=utcnow()
            desc='Synthetic fixture outfit photograph %d. '%i+'The same current factual description is retained without abbreviation.'
            v=VaultItem(account_id=ACCOUNT,media_id=mid,kind='photo',description=desc,
                describe_status='described',describe_generated_at=now,content_hash='large-'+str(i),created_at=now,updated_at=now)
            s.add(v)
            item=CatalogItem(account_id=ACCOUNT,kind='image_set',label=key,description_for_ai=desc,
                media_ids=json.dumps([mid]),preview_media_ids='[]',price_cents=7500,pricing_mode='fixed',
                enabled=True,is_free_teaser=False,created_at=now,updated_at=now)
            s.add(item);s.flush()
            p=deepcopy(settings['products'][0]);p.update(id=key,assets=[{'id':mid,'revision':1,
                'facts':[desc],'evidence_ref':'asset-v2:'+asset_evidence(v)}])
            products.append(p);bindings[key]=item.id
    settings.update(products=products,catalogue_bindings=bindings,sequences={},
        pilot={'fan_ids':[101],'product_ids':[p['id'] for p in products],'supervisor':'fixture-operator'})
    svc.configure(TENANT,ACCOUNT,expected_revision=1,mode='on',settings=settings,actor='fixture-operator')
    identity,model=run(svc,engine,decision('offer','Here is the outfit photo you selected.','large-099'))
    assert status(engine,identity)=='ready'
    payload=json.loads(model.calls[0][1]['content'])
    assert set(payload['products'])==set(bindings)
    assert len(canonical(model.calls[0]).encode())<=81920
    with Session(engine) as s:
        e=s.get(MoxyEvent,(ACCOUNT,identity))
        assert len(e.snapshot_json.encode())>65000
        assert len(json.loads(e.snapshot_json)['inventory'])==100
    intent=svc.queue(TENANT,ACCOUNT,identity)
    transport=SyntheticTextTransport(ACCOUNT)
    assert authority.dispatch(TENANT,ACCOUNT,intent,transport)=='accepted'
    assert transport.calls[0].media_ids==(18099,) and transport.calls[0].price_cents==7500
    store.close()
