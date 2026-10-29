"""Synthetic native preview repair, independent of descriptions and model spend."""
import json
import threading
import pytest
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_onlyfans_vault_pg import sync, media
from test_onlyfans_connections_pg import TENANT
from test_onlyfans_provider_adapters import OWNER, credential
from db.models import VaultItem, VaultFolder, VaultFolderItem
from onlyfans_runtime.provider_connector import NativeProviderConnector
from onlyfans_runtime.vault_preview_worker import VaultPreviewWorker
from onlyfans_runtime.vault_previews import read_preview
from onlyfans_runtime.vault_editing import get_folders
from onlyfans_runtime.store import Refused
import vault_still_store as images

PNG=b"\x89PNG\r\n\x1a\nsynthetic"
URL="https://cdn2.onlyfans.com/files/preview.png?Policy=synthetic"


def test_worker_repairs_oldest_missing_stills_without_description_changes(database,monkeypatch,tmp_path):
    monkeypatch.setattr(images,"IMAGE_DIR",tmp_path)
    store,account,_,_=sync(database,monkeypatch,[media(11,files={"preview":{"url":URL}})])
    with pytest.raises(Refused):read_preview(store,TENANT,account,"11")
    calls=[]
    monkeypatch.setattr(NativeProviderConnector,"vault_media_by_id",lambda self,mid: calls.append(mid) or media(mid,files={"preview":{"url":URL}}))
    monkeypatch.setattr(NativeProviderConnector,"still_bytes",lambda self,url,**kw: PNG)
    worker=VaultPreviewWorker(store,TENANT,threading.Event())
    # A requested gallery page takes priority even after the background cursor.
    worker.cursors[account]=999
    worker.warm(account,requested_only=True)
    assert calls==[11] and read_preview(store,TENANT,account,"11")== (PNG,"image/png")
    worker.cursors.clear();worker.warm(account);assert calls==[11]
    with Session(store.engine) as s:
        row=s.get(VaultItem,(account,11))
        assert row.description is None and row.describe_status is None
    store.close()


def test_worker_refuses_foreign_media_and_preserves_metadata(database,monkeypatch,tmp_path):
    monkeypatch.setattr(images,"IMAGE_DIR",tmp_path)
    store,account,_,_=sync(database,monkeypatch,[media()])
    monkeypatch.setattr(NativeProviderConnector,"vault_media_by_id",lambda self,mid:media(mid,userId=OWNER+1))
    monkeypatch.setattr(NativeProviderConnector,"still_bytes",lambda *a,**kw:pytest.fail("Foreign media fetched"))
    VaultPreviewWorker(store,TENANT,threading.Event()).warm(account)
    with pytest.raises(Refused):read_preview(store,TENANT,account,"11")
    store.close()


def test_import_places_pending_media_in_unsorted_then_classifies_saved_metadata(database,monkeypatch):
    store,account,_,_=sync(database,monkeypatch,[media()])
    monkeypatch.setenv("ONLYFANS_AUTOMATIC_IMPORTS_ENABLED","1")
    from onlyfans_runtime.vault_management import request_sync, process_vault_jobs
    from test_onlyfans_connections_pg import ACTOR
    request_sync(store,TENANT,account,expected_connection_revision=1,command_key="f"*64,actor=ACTOR)
    assert process_vault_jobs(store,TENANT)==1
    folders=get_folders(store,TENANT,account)
    assert len(folders)==1 and folders[0]["name"]=="Unsorted" and folders[0]["count"]==1
    assert folders[0]["automatic"] is True and folders[0]["coverMediaId"]=="11"
    from vault_auto_folders import assign_sync
    with Session(store.engine) as s,s.begin():
        row=s.get(VaultItem,(account,11));row.description="A selfie in a bikini at the beach."
        assign_sync(s,account,11)
    folders=get_folders(store,TENANT,account)
    assert next(f for f in folders if f["name"]=="Unsorted")["count"]==0
    assert {f["name"] for f in folders if f["count"]}=={"Selfies","Bikini / swimwear","Beach / pool"}
    store.close()


def test_single_asset_transport_has_bounded_scope_and_identity_check(monkeypatch):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","worker")
    wire=NativeProviderConnector(credential());calls=[]
    monkeypatch.setattr(wire,"verify_identity",lambda:calls.append("identity"))
    def read(method,path,body=b"",**options):
        assert method=="GET" and path=="/api2/v2/vault/media/11" and body==b""
        assert options["max_response_bytes"]<=1024*1024 and options["accepted_statuses"]==(200,404)
        calls.append("read");return 200,json.dumps(media()).encode()
    monkeypatch.setattr(wire,"_signed_request",read)
    assert wire.vault_media_by_id(11)["id"]==11 and calls==["identity","read"]
    with pytest.raises(Refused):wire.vault_media_by_id(True)


def test_background_classifier_catches_existing_and_later_imports_without_models(database,monkeypatch):
    store,account,_,_=sync(database,monkeypatch,[media()])
    from onlyfans_runtime.vault_folder_worker import classify_batch
    assert classify_batch(store,TENANT,account)==1
    assert next(f for f in get_folders(store,TENANT,account) if f["name"]=="Unsorted")["count"]==1
    assert classify_batch(store,TENANT,account)==0
    with Session(store.engine) as session,session.begin():
        session.get(VaultItem,(account,11)).description="A selfie outdoors."
    assert classify_batch(store,TENANT,account)==1
    folders=get_folders(store,TENANT,account)
    assert {f["name"] for f in folders if f["count"]}=={"Selfies","Outdoors"}
    assert classify_batch(store,TENANT,account)==0
    store.close()
