"""Existing vault owners, real PG, explicit scoped edits; no external services."""
import json
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select,func,text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database,seed
from db.models import VaultItem,VaultFolder,VaultFolderItem
from db.onlyfans_vault_edit_models import OfVaultEdit
from db.schema import CREATOR_FACTS,check_schema,upgrade,SchemaMismatch
from onlyfans_runtime.store import Refused,utcnow
from onlyfans_runtime.vault_editing import get_asset,edit_asset,get_folders,edit_folder
from onlyfans_runtime.vault_management import list_vault,InvalidVault
from vault_ai_effective import effective_value,project_operator_text,normalize_tags
from onlyfans_commands.vault_api import router,SCHEMA,HEADER
TENANT="tenant-a";ACCOUNT="acc-a";ACTOR="op-session:"+"a"*64


def setup(database):
    store=seed(*database)
    with Session(database[1]) as s,s.begin():
        s.add_all([VaultItem(account_id=ACCOUNT,media_id=i,kind="photo",created_at=utcnow(),description="Original description",
            ai_fields_json='{"description":"AI description","tags":["ai"]}',operator_overrides_json='{"unrelated":"retained"}',locked_fields_json='["unrelated"]') for i in (11,12)])
    return store


def asset_save(store,patch,*,data=None,key="b"*64):
    data=data or get_asset(store,TENANT,ACCOUNT,"11")
    return edit_asset(store,TENANT,ACCOUNT,"11",patch=patch,expected_fingerprint=data["fingerprint"],command_key=key,actor=ACTOR)


def folder_save(store,action,*,folder=None,name=None,ids=None,key="c"*64):
    return edit_folder(store,TENANT,ACCOUNT,folder["folderId"] if folder else None,action=action,name=name,media_ids=ids,
        expected_fingerprint=folder["fingerprint"] if folder else None,command_key=key,actor=ACTOR)


def test_shared_projection_preserves_legacy_normalization_and_lock_contract():
    assert normalize_tags(" Coast,COAST\nSunset ")==["coast","sunset"]
    overrides={};locked=set();columns={}
    project_operator_text("tags",["Coast","coast"],overrides,locked,columns)
    assert overrides=={"tags":["coast"]} and locked=={"tags"} and json.loads(columns["tags"])==["coast"]
    project_operator_text("description",None,overrides,locked,columns)
    assert overrides["description"] is None and columns["description"] is None and "description" in locked


def test_existing_metadata_read_precedence_lock_merge_retry_and_conflict(database):
    store=setup(database);initial=get_asset(store,TENANT,ACCOUNT,"11")
    assert initial["description"]=="AI description"
    first=asset_save(store,{"description":"Approved coast","tags":["Coast","coast","SUNSET"]},data=initial)
    assert get_asset(store,TENANT,ACCOUNT,"11")["tags"]==["coast","sunset"]
    later=asset_save(store,{"description":"Corrected coast"},key="d"*64)
    assert asset_save(store,{"description":"Approved coast","tags":["Coast","coast","SUNSET"]},data=initial)==first
    assert first!=later
    with pytest.raises(Refused):asset_save(store,{"description":"Stale write"},data=initial,key="e"*64)
    with Session(database[1]) as s,s.begin():
        row=s.get(VaultItem,(ACCOUNT,11));row.ai_fields_json='{"description":"Forced AI replacement"}'
        assert effective_value(row,"description")=="Corrected coast"
        assert json.loads(row.operator_overrides_json)["unrelated"]=="retained"
        assert set(json.loads(row.locked_fields_json))=={"unrelated","description","tags"}
        assert row.description=="Corrected coast" and json.loads(row.tags)==["coast","sunset"]
    for patch in ({"price_cents":0},{"tags":["x"]*13},{"description":True},{"description":"x"*2001}):
        with pytest.raises(InvalidVault):asset_save(store,patch,key="f"*64)
    with pytest.raises(DBAPIError,match="append-only"):
        with database[1].begin() as c:c.exec_driver_sql("DELETE FROM of_vault_edit")
    store.close()


def test_concurrent_native_metadata_updates_serialize_without_lost_edit(database):
    store=setup(database);data=get_asset(store,TENANT,ACCOUNT,"11")
    def update(key):
        try:return asset_save(store,{"description":key},data=data,key=key*64)
        except Refused:return "refused"
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(update,["a","b"]))
    assert results.count("refused")==1
    with Session(database[1]) as s:assert s.scalar(select(func.count()).select_from(OfVaultEdit))==1
    store.close()


def test_folders_use_existing_membership_scoped_filter_versions_and_archive(database):
    store=setup(database)
    created=folder_save(store,"create",name="Coast")
    folder=get_folders(store,TENANT,ACCOUNT,created["targetId"])
    added=folder_save(store,"add",folder=folder,ids=["11","12"],key="d"*64)
    assert folder_save(store,"add",folder=folder,ids=["11","12"],key="d"*64)==added
    assert [r["mediaId"] for r in list_vault(store,TENANT,ACCOUNT,folder_id=folder["folderId"])["items"]]==["11","12"]
    assert get_folders(store,TENANT,ACCOUNT)==[{"folderId":folder["folderId"],"name":"Coast","count":2,"coverMediaId":"11","coverKind":"photo","automatic":False}]
    with pytest.raises(Refused):folder_save(store,"rename",folder=folder,name="Stale",key="e"*64)
    folder=get_folders(store,TENANT,ACCOUNT,folder["folderId"])
    folder_save(store,"remove",folder=folder,ids=["12"],key="f"*64)
    folder=get_folders(store,TENANT,ACCOUNT,folder["folderId"])
    folder_save(store,"archive",folder=folder,key="a"*64)
    assert get_folders(store,TENANT,ACCOUNT)==[]
    with pytest.raises(Refused):list_vault(store,TENANT,ACCOUNT,folder_id=folder["folderId"])
    with Session(database[1]) as s:
        assert s.scalar(select(func.count()).select_from(VaultFolderItem))==1  # archived, not deleted
        assert s.scalar(select(VaultFolder)).of_list_id is None
    store.close()


def test_cross_account_refs_and_corrupt_metadata_refuse_before_any_edit(database):
    store=setup(database);other=seed(*database,account="acc-b",tenant="tenant-b")
    with Session(database[1]) as s,s.begin():s.add(VaultItem(account_id="acc-b",media_id=99,kind="photo",created_at=utcnow()))
    created=folder_save(store,"create",name="Scoped")
    folder=get_folders(store,TENANT,ACCOUNT,created["targetId"])
    with pytest.raises(Refused):folder_save(store,"add",folder=folder,ids=["11","99"],key="d"*64)
    with pytest.raises(Refused):get_asset(store,"tenant-b",ACCOUNT,"11")
    with pytest.raises(Refused):get_folders(store,"tenant-b","acc-b",folder["folderId"])
    with Session(database[1]) as s,s.begin():s.get(VaultItem,(ACCOUNT,11)).operator_overrides_json='["corrupt"]'
    with pytest.raises(InvalidVault):asset_save(store,{"description":"Must not replace private saved data"})
    with Session(database[1]) as s:assert s.scalar(select(func.count()).select_from(VaultFolderItem))==0
    store.close();other.close()


def test_api_auth_exact_commands_and_historical_acknowledgements(database,monkeypatch):
    store=setup(database);monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","api")
    monkeypatch.setenv("ONLYFANS_CONTENT_TOKEN","synthetic-content");monkeypatch.setenv("ONLYFANS_CONTENT_TENANT",TENANT)
    app=FastAPI();app.include_router(router);app.state.control_store=store;client=TestClient(app)
    path=f"/onlyfans/content/v1/accounts/{ACCOUNT}/vault/assets/11";headers={"Authorization":"Bearer synthetic-content",HEADER:SCHEMA}
    assert client.get(path).status_code==401
    result=client.get(path,headers=headers);assert result.status_code==200,result.text
    body={"patch":{"description":"API approved"},"expectedFingerprint":result.json()["asset"]["fingerprint"],"commandKey":"b"*64,"actor":ACTOR}
    for bad in ({**body,"extra":True},{**body,"patch":{"kind":"video"}}):assert client.post(path,headers=headers,json=bad).status_code==400
    first=client.post(path,headers=headers,json=body);assert first.status_code==200,first.text
    assert client.post(path,headers=headers,json=body).json()==first.json()
    assert client.get(path+"?unknown=true",headers=headers).status_code==400
    assert client.get(path.replace("11","99"),headers=headers).status_code==404
    store.close()


def test_populated_shared_fact_upgrade_preserves_metadata_and_checks_immutable_commands(database):
    store=setup(database);store.close()
    with database[1].begin() as c:
        from test_onlyfans_offer_import_upgrade_pg import historical_schema
        historical_schema(c,CREATOR_FACTS)
        before=c.exec_driver_sql("SELECT row_to_json(t) FROM vault_items t ORDER BY media_id").scalars().all()
        upgrade(c);upgrade(c);check_schema(c)
        assert before==c.exec_driver_sql("SELECT row_to_json(t) FROM vault_items t ORDER BY media_id").scalars().all()
        c.exec_driver_sql("ALTER TABLE of_vault_edit DISABLE TRIGGER of_vault_edit_immutable")
        with pytest.raises(SchemaMismatch,match="append-only"):check_schema(c)

