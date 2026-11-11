"""Agency-only category pricing via existing vault/HTTP authority; isolated PG/fakes."""
import copy
import json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from db.models import Account, CatalogItem, VaultFolder, VaultFolderItem
from db.onlyfans_vault_models import OfVaultProductCreate
from db.onlyfans_models import OfRuntime
from onlyfans_runtime.vault_management import create_category_drafts, InvalidVault
from onlyfans_runtime.store import Refused, utcnow
from onlyfans_commands.vault_api import router, HEADER, SCHEMA
from test_onlyfans_foundation_pg import database
from test_onlyfans_vault_pg import sync
from test_onlyfans_connections_pg import TENANT, ACTOR

@pytest.fixture
def prepared(database, monkeypatch):
    store, account, _, calls = sync(database, monkeypatch)
    with Session(store.engine) as s, s.begin():
        folder = VaultFolder(account_id=account, name="Scenic photos", created_by=ACTOR)
        s.add(folder); s.flush(); fid=folder.id
        s.add_all([VaultFolderItem(account_id=account,folder_id=fid,media_id=mid) for mid in (11,12)])
    value={"folderId":fid,"priceCents":1200,"classificationConfirmed":True,"products":[
        {"label":"Coast one","description":"A scenic photograph.","mediaIds":["11"],"previewMediaIds":[]},
        {"label":"Coast two","description":"Another scenic photograph.","mediaIds":["12"],"previewMediaIds":[]}]}
    from onlyfans_runtime.vault_editing import get_category_review
    value["reviewedState"]=get_category_review(store,TENANT,account,fid,["11","12"])
    value["rightsConfirmed"]=True
    yield store,account,value,calls
    store.close()

def run(fixture, value=None, key="e"*64, actor=ACTOR, tenant=TENANT):
    store,account,category,_=fixture
    return create_category_drafts(store,tenant,account,category=category if value is None else value,
        command_key=key,actor=actor)

def count(store, model):
    with Session(store.engine) as s:return s.scalar(select(func.count()).select_from(model))

def test_agency_batch_is_fixed_disabled_audited_and_replayable(prepared):
    store,account,_,calls=prepared
    first=run(prepared)
    assert len(first["items"])==2 and run(prepared)==first
    with Session(store.engine) as s:
        rows=list(s.scalars(select(CatalogItem)))
        assert len(rows)==2 and all(r.price_cents==1200 and r.pricing_mode=="fixed" and not r.enabled for r in rows)
        assert s.get(OfRuntime,account).enabled is False
        assert s.scalar(select(func.count()).select_from(OfVaultProductCreate))==2
    assert calls==[(0,24)]  # only the synthetic setup read; batch uses no connector/model

@pytest.mark.parametrize("field,value",[("priceCents",None),("priceCents",True),("priceCents",299),
    ("priceCents",20001),("classificationConfirmed",False),("folderId",True)])
def test_no_guessed_or_invalid_agency_price(prepared,field,value):
    category=copy.deepcopy(prepared[2]);category[field]=value
    with pytest.raises(InvalidVault):run(prepared,category)
    assert count(prepared[0],CatalogItem)==0

@pytest.mark.parametrize("name",["Miscellaneous"," MISCELLANEOUS ","Unknown"])
def test_miscellaneous_cannot_become_approved_products(prepared,name):
    store,account,category,_=prepared
    with Session(store.engine) as s,s.begin():s.get(VaultFolder,category["folderId"]).name=name
    with pytest.raises(Refused,match="uncertain category"):run(prepared)
    assert count(store,CatalogItem)==0

def test_changed_membership_rolls_back_the_entire_batch(prepared):
    store,account,category,_=prepared
    with Session(store.engine) as s,s.begin():
        s.delete(s.get(VaultFolderItem,(account,category["folderId"],12)))
    with pytest.raises(Refused,match="membership changed"):run(prepared)
    assert count(store,CatalogItem)==count(store,OfVaultProductCreate)==0

def test_second_duplicate_product_cannot_leave_first_partially_created(prepared):
    store,account,category,_=prepared
    # Existing second asset must cause atomic failure after first draft was flushed.
    from test_onlyfans_vault_pg import create
    create(store,account,mediaIds=["12"],previewMediaIds=[])
    before=count(store,CatalogItem)
    with pytest.raises(Refused,match="already present"):run(prepared)
    assert count(store,CatalogItem)==before and count(store,OfVaultProductCreate)==1

def test_replay_cannot_change_price_or_actor(prepared):
    first=run(prepared);category=copy.deepcopy(prepared[2]);category["priceCents"]=1500
    with pytest.raises(Refused,match="idempotency"):run(prepared,category)
    with pytest.raises(Refused,match="idempotency"):run(prepared,actor="op-session:"+"f"*64)
    assert run(prepared)==first

def test_foreign_folder_and_tenant_cannot_leak_or_create(prepared):
    store,account,category,_=prepared
    with Session(store.engine) as s,s.begin():
        s.add(Account(id="other",nickname="Other creator"));s.flush()
        folder=VaultFolder(account_id="other",name="Scenic photos",created_by=ACTOR)
        s.add(folder);s.flush();other_id=folder.id
    value=copy.deepcopy(category);value["folderId"]=other_id
    with pytest.raises(Refused,match="folder unavailable"):run(prepared,value)
    with pytest.raises(Refused):run(prepared,tenant="other-tenant")
    assert count(store,CatalogItem)==0

def test_background_import_cannot_set_agency_prices(prepared):
    with pytest.raises(Refused,match="agency pricing"):run(prepared,actor="worker-import:"+"a"*64)
    assert count(prepared[0],CatalogItem)==0

def test_existing_authenticated_http_route_requires_credential_and_contract(prepared,monkeypatch):
    store,account,category,_=prepared
    monkeypatch.setenv("ONLYFANS_CONTENT_TOKEN","synthetic-content")
    monkeypatch.setenv("ONLYFANS_CONTENT_TENANT",TENANT)
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","api")
    app=FastAPI();app.state.control_store=store;app.include_router(router)
    path=f"/onlyfans/content/v1/accounts/{account}/vault/category-drafts"
    body={"category":category,"commandKey":"d"*64,"actor":ACTOR}
    with TestClient(app) as client:
        assert client.post(path,json=body).status_code==401
        headers={"Authorization":"Bearer synthetic-content",HEADER:SCHEMA}
        result=client.post(path,json=body,headers=headers)
        assert result.status_code==200,result.text
        assert len(result.json()["items"])==2


def test_concurrent_same_batch_creates_exactly_one_set_of_drafts(prepared):
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy import text
    import time
    from onlyfans_runtime.store import Store
    store=Store(str(prepared[0].engine.url),pool_size=4)
    parallel=(store,*prepared[1:])
    with ThreadPoolExecutor(max_workers=2) as pool:
        with Session(store.engine) as s,s.begin():
            store._runtime(s,TENANT,prepared[1])
            futures=[pool.submit(run,parallel) for _ in range(2)]
            deadline=time.monotonic()+10
            while True:
                with store.engine.connect() as c:
                    blocked=c.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'"))
                if blocked>=2:break
                assert time.monotonic()<deadline,"Both commands must contend on the actual PG runtime lock"
                time.sleep(.02)
        a,b=[future.result(timeout=15) for future in futures]
    assert a==b
    assert count(store,CatalogItem)==count(store,OfVaultProductCreate)==2
    store.close()


def test_drafts_cannot_be_sent_and_fixed_terms_reject_price_override(prepared):
    from onlyfans_runtime.media_delivery import catalog_terms
    result=run(prepared);store,account,_,_=prepared
    item=int(result["items"][0]["itemId"])
    with Session(store.engine) as s:
        with pytest.raises(Refused,match="approved catalogue terms unavailable"):
            catalog_terms(s,account,item)
    # Synthetic authority check only: activating a product is a separate later action.
    with Session(store.engine) as s,s.begin():s.get(CatalogItem,item).enabled=True
    with Session(store.engine) as s:
        assert catalog_terms(s,account,item)["price_cents"]==1200
        with pytest.raises(Refused):catalog_terms(s,account,item,price_cents=1300)
        with pytest.raises(Refused):catalog_terms(s,account,item,price_cents=1100)


@pytest.mark.parametrize("manage,creators",[(False,("creator-new",)),(True,("another-creator",))])
def test_agency_capability_and_creator_assignment_are_required(prepared,manage,creators):
    from onlyfans_read.agency_scope import AgencyScope,current_scope
    actor="agency-user:"+"a"*8+"-"+"a"*4+"-"+"a"*4+"-"+"a"*4+"-"+"a"*12+":"+"b"*8+"-"+"b"*4+"-"+"b"*4+"-"+"b"*4+"-"+"b"*12
    token=current_scope.set(AgencyScope(creators,actor,manage))
    try:
        with pytest.raises((InvalidVault,Refused)):run(prepared,actor=actor)
    finally:
        current_scope.reset(token)
    assert count(prepared[0],CatalogItem)==0


@pytest.mark.parametrize("change",["name","description","hash","classification","binding"])
def test_reviewed_state_must_match_before_draft_creation(prepared,change):
    from db.models import VaultItem
    store,account,category,_=prepared
    with Session(store.engine) as s,s.begin():
        row=s.get(VaultItem,(account,11))
        if change=="name":s.get(VaultFolder,category["folderId"]).name="New category meaning"
        elif change=="description":row.description="A corrected factual description."
        elif change=="hash":row.content_hash="different-content"
        elif change=="classification":row.ai_fields_json='{"primary_folder":"uncertain"}'
        else:
            category["reviewedState"]["bindingId"]="obsolete-binding"
    with pytest.raises(Refused,match="reviewed"):run(prepared)
    assert count(store,CatalogItem)==0


def enable(prepared,item):
    from onlyfans_runtime.content_management import update_content,get_content_v2
    store,account,_,_=prepared
    data=get_content_v2(store,TENANT,account,item)
    return update_content(store,TENANT,account,item,patch={"enabled":True},
        expected_revision=data["revision"],expected_fingerprint=data["fingerprint"],
        command_key="9"*64,actor=ACTOR,version=2)


def test_unrelated_addition_keeps_review_valid_through_enable(prepared):
    store,account,category,_=prepared
    with Session(store.engine) as s,s.begin():
        s.add(VaultFolderItem(account_id=account,folder_id=category["folderId"],media_id=13))
    result=run(prepared)
    for row in result["items"]:
        enable(prepared,int(row["itemId"]))


@pytest.mark.parametrize("stage",["enable","terms"])
def test_selected_change_invalidates_existing_draft_or_unsent_terms(prepared,stage):
    from db.models import VaultItem
    from onlyfans_runtime.media_delivery import catalog_terms
    result=run(prepared);store,account,_,_=prepared
    item=int(result["items"][0]["itemId"])
    if stage=="terms":enable(prepared,item)
    with Session(store.engine) as s,s.begin():s.get(VaultItem,(account,11)).content_hash="changed"
    with pytest.raises(Refused,match="reviewed"):
        if stage=="enable":enable(prepared,item)
        else:
            with Session(store.engine) as s:catalog_terms(s,account,item)


def test_renamed_uncertain_auto_folder_is_not_a_classification(prepared):
    from vault_auto_folders import OWNER_PREFIX
    store,account,category,_=prepared
    with Session(store.engine) as s,s.begin():
        s.get(VaultFolder,category["folderId"]).created_by=OWNER_PREFIX+"uncategorised"
    with pytest.raises(Refused,match="uncertain category"):run(prepared)


def test_bundle_needs_explicit_total_not_category_single_price(prepared):
    value=copy.deepcopy(prepared[2])
    value["products"]=[{**value["products"][0],"mediaIds":["11","12"]}]
    with pytest.raises(InvalidVault,match="bundle"):run(prepared,value)
    value["products"][0]["approvedTotalCents"]=1800
    result=run(prepared,value)
    assert len(result["items"])==1 and result["items"][0]["item"]["priceCents"]==1800


def test_disabled_draft_fails_actual_common_sender_preparation(prepared):
    from db.models import Fan,OfConversationState
    from db.onlyfans_delivery_models import OfTextIntent
    from onlyfans_runtime.text_delivery import TextAuthority,SyntheticTextTransport
    store,account,_,_=prepared
    item=int(run(prepared)["items"][0]["itemId"])
    with Session(store.engine) as s,s.begin():
        runtime=s.get(OfRuntime,account)
        runtime.enabled=True;runtime.recovery_needed=False;runtime.last_sync_at=utcnow()
        s.add(Fan(account_id=account,fan_id=101));s.flush()
        s.add(OfConversationState(account_id=account,fan_id=101,mode="human_controlled",
            revision=0,takeover_state="none",requires_attention=False))
    authority=TextAuthority(store)
    authority.configure(TENANT,account,expected_revision=0,enabled=True,media_enabled=True,
        limit_units=5,throttle_seconds=1,actor=ACTOR,reason="Isolated sender negative control")
    wire=SyntheticTextTransport(account)
    with pytest.raises(Refused,match="approved catalogue"):
        identity=authority.prepare(TENANT,account,101,command_key="disabled",expected_revision=0,
            body="The reviewed photo.",origin="manual",actor=ACTOR,catalog_item_id=item)
        authority.dispatch(TENANT,account,identity,wire)
    assert not wire.calls and count(store,OfTextIntent)==0


def test_actual_agency_middleware_checks_namespace_contract_actor_and_assignment(prepared,monkeypatch):
    from onlyfans_read.agency_scope import AgencyScopeMiddleware,HEADER as SCOPE_HEADER
    from db.models import OfCreatorBinding
    creator="11111111-1111-4111-8111-111111111111"
    actor="agency-user:33333333-3333-4333-8333-333333333333:44444444-4444-4444-8444-444444444444"
    store,account,category,_=prepared
    with Session(store.engine) as s,s.begin():
        runtime=s.get(OfRuntime,account);s.get(OfCreatorBinding,runtime.binding_id).creator_id=creator
    monkeypatch.setenv("ONLYFANS_CONTENT_TOKEN","synthetic-content")
    monkeypatch.setenv("ONLYFANS_CONTENT_TENANT",TENANT);monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","api")
    app=FastAPI();app.state.control_store=store;app.add_middleware(AgencyScopeMiddleware);app.include_router(router)
    path=f"/onlyfans/agency/v1/content/v1/accounts/{account}/vault/category-drafts"
    payload={"category":category,"commandKey":"d"*64,"actor":actor}
    headers={"Authorization":"Bearer synthetic-content",HEADER:SCHEMA}
    grant={"creatorIds":[creator],"actor":actor,"canManage":True}
    with TestClient(app) as client:
        assert client.post(path,json=payload,headers=headers).status_code==403
        scoped={**headers,SCOPE_HEADER:json.dumps(grant)}
        for schema in (None,"wrong"):
            denied=dict(scoped)
            if schema is None:denied.pop(HEADER)
            else:denied[HEADER]=schema
            assert client.post(path,json=payload,headers=denied).status_code==400
        for changed in ({"canManage":False},{"creatorIds":[]},{"creatorIds":["22222222-2222-4222-8222-222222222222"]}):
            denied={**headers,SCOPE_HEADER:json.dumps({**grant,**changed})}
            assert client.post(path,json=payload,headers=denied).status_code in (403,404)
        assert client.post(path,json={**payload,"actor":ACTOR},headers=scoped).status_code==400
        assert count(store,CatalogItem)==0
        result=client.post(path,json=payload,headers=scoped)
        assert result.status_code==200,result.text
        assert len(result.json()["items"])==2


def test_current_readiness_cannot_be_bypassed_by_saved_classification(prepared):
    from db.models import VaultItem
    item=int(run(prepared)["items"][0]["itemId"])
    store,account,_,_=prepared
    with Session(store.engine) as s,s.begin():
        row=s.get(VaultItem,(account,11))
        raw=json.loads(row.raw_json);raw["isReady"]=False;row.raw_json=json.dumps(raw)
    with pytest.raises(Refused,match="readiness"):enable(prepared,item)
