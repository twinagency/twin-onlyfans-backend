"""Owned PG, synthetic stills and no real CDN/account/model calls."""
from datetime import timedelta
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from test_onlyfans_vault_pg import sync, media, page
from test_onlyfans_connections_pg import verified, TENANT
from test_onlyfans_provider_adapters import OWNER, credential, fake_curl
from db.models import VaultItem, OfCreatorBinding
from db.onlyfans_models import OfRuntime
from db.onlyfans_connection_models import OfProviderConnection
from onlyfans_commands.vault_api import router, SCHEMA, HEADER
from onlyfans_runtime.provider_connector import NativeProviderConnector
from onlyfans_runtime.native_protocol import NativeProtocolError
from onlyfans_runtime.store import Refused, utcnow
from onlyfans_runtime.vault_previews import read_preview, warm_page
from onlyfans_runtime.vault_management import request_sync, process_vault_jobs
import vault_still_store as images

PNG = b"\x89PNG\r\n\x1a\nsynthetic-owned-image"
URL = "https://cdn2.onlyfans.com/files/preview.png?Policy=synthetic"


def test_sync_warms_existing_store_and_reuses_it_without_models_or_second_fetch(database,monkeypatch,tmp_path):
    monkeypatch.setattr(images,"IMAGE_DIR",tmp_path)
    monkeypatch.setenv("ONLYFANS_VAULT_PREVIEWS_ENABLED","1")
    calls=[]
    def fetch(self,url):
        assert url==URL and self.account_id=="of-"+str(OWNER)
        calls.append(url);return PNG
    monkeypatch.setattr(NativeProviderConnector,"still_bytes",fetch)
    row=media(files={"full":{"url":URL},"preview":{"url":URL}})
    store,account,_,_=sync(database,monkeypatch,[row])
    assert calls==[URL]
    assert images.image_path(account,11).read_bytes()==PNG
    def no_credentials(connection,cursor,statement,parameters,context,many):
        assert "sealed_config" not in statement.lower(),"Preview reads must not load encrypted provider credentials"
    event.listen(store.engine,"before_cursor_execute",no_credentials)
    try: assert read_preview(store,TENANT,account,"11")== (PNG,"image/png")
    finally: event.remove(store.engine,"before_cursor_execute",no_credentials)
    request_sync(store,TENANT,account,expected_connection_revision=1,command_key="f"*64,actor="op-session:"+"a"*64)
    assert process_vault_jobs(store,TENANT)==1 and calls==[URL]
    with pytest.raises(Refused):read_preview(store,"other-tenant",account,"11")
    with Session(store.engine) as s,s.begin():
        connection=s.get(OfProviderConnection,account)
        connection.expires_at=utcnow()-timedelta(seconds=1)
        connection.status="paused";connection.revision+=1
    assert read_preview(store,TENANT,account,"11")== (PNG,"image/png"),"Paused/expired credentials do not erase owned cached pictures"
    with Session(store.engine) as s,s.begin():s.get(OfCreatorBinding,s.get(OfRuntime,account).binding_id).revoked_at=utcnow()
    with pytest.raises(Refused):read_preview(store,TENANT,account,"11")
    store.close()


def test_no_unproven_foreign_corrupt_hidden_or_removed_cache_is_exposed(database,monkeypatch,tmp_path):
    monkeypatch.setattr(images,"IMAGE_DIR",tmp_path)
    store,account,_,_=sync(database,monkeypatch)
    path=images.image_path(account,11);images.write(path,PNG)
    with pytest.raises(Refused):read_preview(store,TENANT,account,"11")
    class Wire:
        account_id=account
        def still_bytes(self,url):return PNG
    with Session(store.engine) as s:binding=s.get(OfRuntime,account).binding_id
    assert warm_page(Wire(),[media(files={"preview":{"url":URL}})],binding_id=binding,owner_id=OWNER)==1
    receipt=path.with_suffix(".native.json");proof=json.loads(receipt.read_text())
    receipt.write_text(json.dumps({**proof,"ownerId":OWNER+1}))
    with pytest.raises(Refused):read_preview(store,TENANT,account,"11")
    receipt.write_text(json.dumps(proof));path.with_suffix(".owned.jpg").write_bytes(PNG+b"corrupt")
    with pytest.raises(Refused):read_preview(store,TENANT,account,"11")
    path.with_suffix(".owned.jpg").write_bytes(PNG)
    with Session(store.engine) as s,s.begin():s.get(VaultItem,(account,11)).removed_at=utcnow()
    with pytest.raises(Refused):read_preview(store,TENANT,account,"11")
    store.close()


def test_cached_preview_http_is_private_purpose_authenticated_and_never_fetches_provider(database,monkeypatch,tmp_path):
    monkeypatch.setattr(images,"IMAGE_DIR",tmp_path)
    monkeypatch.setenv("ONLYFANS_VAULT_PREVIEWS_ENABLED","1")
    monkeypatch.setattr(NativeProviderConnector,"still_bytes",lambda *_:PNG)
    store,account,_,_=sync(database,monkeypatch)
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","api")
    monkeypatch.setenv("ONLYFANS_CONTENT_TOKEN","synthetic-content-only")
    monkeypatch.setenv("ONLYFANS_CONTENT_TENANT",TENANT)
    monkeypatch.setattr(NativeProviderConnector,"still_bytes",lambda *_:pytest.fail("API attempted provider I/O"))
    app=FastAPI();app.state.control_store=store;app.include_router(router)
    client=TestClient(app);url=f"/onlyfans/content/v1/accounts/{account}/vault/previews/11"
    headers={"authorization":"Bearer synthetic-content-only",HEADER:SCHEMA}
    assert client.get(url).status_code==401
    assert client.get(url,headers={**headers,HEADER:"wrong"}).status_code==400
    answer=client.get(url,headers=headers)
    assert answer.status_code==200 and answer.content==PNG
    assert answer.headers["content-type"]=="image/png"
    assert answer.headers["cache-control"]=="private, no-store" and answer.headers["x-content-type-options"]=="nosniff"
    assert client.get(url+"?url=https://caller.example",headers=headers).status_code==400
    assert client.get(url[:-2]+"999",headers=headers).status_code==404
    assert client.get(url.replace(account,"other-account"),headers=headers).status_code==404
    store.close()


@pytest.mark.parametrize("url",["http://cdn2.onlyfans.com/a.png","https://cdn.onlyfans.attacker.com/a.png",
    "https://onlyfans.com/a.png","https://127.0.0.1/a.png","https://cdn2.onlyfans.com:444/a.png",
    "https://user:secret@cdn2.onlyfans.com/a.png","https://cdn2.onlyfans.com/a.png#fragment"])
def test_cdn_targets_refused_before_network(monkeypatch,url):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","worker");calls=fake_curl(monkeypatch)
    with pytest.raises(Refused):NativeProviderConnector(credential()).still_bytes(url)
    assert calls==[]


def test_cdn_get_has_no_credentials_retries_redirects_or_unbounded_body(monkeypatch):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","worker")
    calls=fake_curl(monkeypatch,body=PNG,headers={"content-type":"image/png"})
    wire=NativeProviderConnector(credential(proxy_url="http://fixed.example:8080"))
    assert wire.still_bytes(URL)==PNG
    request=next(row for row in calls if "url" in row)
    assert request["method"]=="GET" and request["options"]["proxy"]=="http://fixed.example:8080"
    assert set(request["options"]["headers"])=={"Accept","Accept-Encoding","User-Agent","Referer"}
    assert request["options"]["timeout"]<=2 and request["options"]["allow_redirects"] is False
    assert calls[0]["constructor"]["retry"]==0
    for options in [{"status":302},{"body":b"<html>"},{"headers":{"content-type":"image/svg+xml"}},
                    {"headers":{"content-type":"image/png","content-encoding":"gzip"}},
                    {"body":PNG+b"x"*images.MAX_STILL_BYTES}]:
        args={"body":PNG,"headers":{"content-type":"image/png"},**options}
        calls=fake_curl(monkeypatch,**args)
        with pytest.raises(NativeProtocolError):wire.still_bytes(URL)
        assert len([row for row in calls if "url" in row])==1
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","api");calls=fake_curl(monkeypatch)
    with pytest.raises(Refused):wire.still_bytes(URL)
    assert calls==[]



def test_legacy_caption_cache_replacement_cannot_blank_verified_gallery(database,monkeypatch,tmp_path):
    monkeypatch.setattr(images,"IMAGE_DIR",tmp_path)
    monkeypatch.setenv("ONLYFANS_VAULT_PREVIEWS_ENABLED","1")
    monkeypatch.setattr(NativeProviderConnector,"still_bytes",lambda *_:PNG)
    store,account,_,_=sync(database,monkeypatch,[media()])
    path=images.image_path(account,11)
    path.write_bytes(PNG+b"legacy-caption-variant")
    assert read_preview(store,TENANT,account,"11")== (PNG,"image/png")
    # API reads stay read-only and also cannot expose a corrupted gallery copy.
    path.with_suffix(".owned.jpg").write_bytes(PNG+b"corrupt")
    with pytest.raises(Refused):read_preview(store,TENANT,account,"11")
    store.close()
