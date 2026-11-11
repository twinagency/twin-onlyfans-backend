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
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(run,prepared) for _ in range(2)]
        a,b=[future.result() for future in futures]
    assert a==b
    assert count(prepared[0],CatalogItem)==count(prepared[0],OfVaultProductCreate)==2


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
