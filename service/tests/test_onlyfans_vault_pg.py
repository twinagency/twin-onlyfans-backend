"""Owned PostgreSQL and synthetic native vault pages; no real credentials/network."""
import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from test_onlyfans_connections_pg import verified, ACTOR, TENANT
from test_onlyfans_provider_adapters import OWNER
from db.models import CatalogItem, VaultItem, OfCreatorBinding
from db.onlyfans_models import OfRuntime
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_vault_models import OfVaultSyncJob, OfVaultObservation, OfVaultProductCreate
from db.schema import check_schema, SchemaMismatch
from onlyfans_runtime.content_management import get_content, update_content
from onlyfans_runtime.native_protocol import NativeProtocolError
from onlyfans_runtime.provider_connector import NativeProviderConnector
from onlyfans_runtime.store import Refused, utcnow
from onlyfans_runtime.vault_management import (InvalidVault, create_product, list_vault,
    parse_vault_page, process_vault_jobs, request_sync, validate_product)
from onlyfans_commands.vault_api import router, HEADER, SCHEMA


def media(mid=11, kind="photo", **changes):
    return {"id": mid, "type": kind, "createdAt": "2026-10-18T10:00:00+10:00", "isReady": True,
        "width": 640, "height": 800, "duration": 62.5 if kind == "video" else None,
        "files": {"full": {"url": "https://synthetic-cdn.example/full"},
                  "thumb": {"url": "https://synthetic-cdn.example/thumb"}}, **changes}


def page(rows, more=False):
    return json.dumps({"list": rows, "hasMore": more}).encode()


def queue(store, account, key="c" * 64):
    return request_sync(store, TENANT, account, expected_connection_revision=1, command_key=key, actor=ACTOR)


def sync(database, monkeypatch, rows=None):
    store, account, _ = verified(database, monkeypatch)
    job = queue(store, account)
    payload = page(rows if rows is not None else [media(), media(12), media(13, "video")])
    calls = []
    def read(self, *, offset, limit=24):
        assert self.account_id == account and offset == 0 and limit == 24
        assert store.engine.pool.checkedout() == 0, "HTTP must not span a database transaction"
        calls.append((offset, limit))
        return payload
    monkeypatch.setattr(NativeProviderConnector, "vault_page", read)
    assert process_vault_jobs(store, TENANT) == 1
    return store, account, job, calls


def product(**patch):
    return {"label": "Scenic collection", "description": "Two approved scenic photographs.",
        "priceCents": 1000, "enabled": True, "mediaIds": ["11", "12"], "previewMediaIds": ["11"], **patch}


def create(store, account, key="d" * 64, **patch):
    return create_product(store, TENANT, account, product=product(**patch), command_key=key, actor=ACTOR)


@pytest.mark.parametrize("patch", [{"id": True}, {"id": "11"}, {"type": "unknown"},
    {"isReady": 1}, {"isProcessing": "false"}, {"createdAt": "2026-10-18T00:00:00"},
    {"userId": OWNER + 1}, {"files": {"full": {"url": "http://caller.example"}}},
    {"width": -1}, {"duration": float("inf")}])
def test_strict_native_page_never_invents_owner_readiness_or_shape(patch):
    with pytest.raises(NativeProtocolError):
        parse_vault_page(page([media(**patch)]), owner_id=OWNER)


def test_atomic_native_page_bounds_and_ambiguity():
    for raw in [b'{"list":[],"list":[],"hasMore":false}', page([media()], True),
                page([media(), media()]), page([media(i + 1) for i in range(25)]), b" " * 65537]:
        with pytest.raises(NativeProtocolError): parse_vault_page(raw, owner_id=OWNER)
    missing = media(); del missing["isReady"]
    assert parse_vault_page(page([missing]), owner_id=OWNER)[0][0].get("isReady") is None


def test_new_disabled_account_sync_uses_native_owner_without_outgoing_or_llm(database, monkeypatch):
    store, account, job, calls = sync(database, monkeypatch)
    result = list_vault(store, TENANT, account, limit=2)
    assert result["sync"]["status"] == "complete" and result["sync"]["itemsSeen"] == 3
    assert result["nextCursor"] == "12" and result["connectionRevision"] == 1
    assert all(row["ready"] for row in result["items"])
    assert [row["mediaId"] for row in list_vault(store, TENANT, account, after_media_id="12")["items"]] == ["13"]
    assert queue(store, account)["jobId"] == job["jobId"] and calls == [(0, 24)]
    with Session(database[1]) as s:
        assert s.get(OfRuntime, account).enabled is False
        assert s.get(VaultItem, (account, 11)).created_at.hour == 0, "native timezone normalized to UTC"
        assert s.scalar(select(func.count()).select_from(OfVaultObservation)) == 3
    with pytest.raises(Refused): list_vault(store, "other-tenant", account)
    with pytest.raises(Refused): request_sync(store, TENANT, account, expected_connection_revision=2, command_key="e" * 64, actor=ACTOR)
    store.close()


def test_native_search_uses_existing_tags_notes_and_effective_description_without_wildcard_or_scope_leaks(database, monkeypatch):
    store, account, _, _ = sync(database, monkeypatch)
    with Session(database[1]) as s, s.begin():
        s.get(VaultItem, (account, 11)).tags = '["coast","sunset"]'
        s.get(VaultItem, (account, 12)).notes = "Coast, 100% approved"
        row = s.get(VaultItem, (account, 13))
        row.description = "legacy description"
        row.operator_overrides_json = '{"description":"Approved coastal video"}'
        row.locked_fields_json = '["description"]'
    assert [r["mediaId"] for r in list_vault(store, TENANT, account, query="COAST", limit=1)["items"]] == ["11"]
    page = list_vault(store, TENANT, account, query="coast", after_media_id="11")
    assert [r["mediaId"] for r in page["items"]] == ["12", "13"]
    assert [r["mediaId"] for r in list_vault(store, TENANT, account, query="%" )["items"]] == ["12"]
    video = list_vault(store, TENANT, account, kind="video")["items"]
    assert len(video) == 1 and video[0]["description"] == "Approved coastal video"
    assert list_vault(store, TENANT, account, query="no-such-tag")["items"] == []
    with pytest.raises(Refused): list_vault(store, "other-tenant", account, query="coast")
    for patch in [{"query":"x"*121}, {"query":"\x00"}, {"kind":"unknown"}]:
        with pytest.raises(InvalidVault): list_vault(store, TENANT, account, **patch)
    store.close()


def test_sync_freezes_request_replays_and_one_active_job(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    first = queue(store, account)
    assert queue(store, account) == first
    with pytest.raises(Refused, match="already active"): queue(store, account, "e" * 64)
    with pytest.raises(Refused, match="idempotency"):
        request_sync(store, TENANT, account, expected_connection_revision=2, command_key="c" * 64, actor=ACTOR)
    for statement in ["UPDATE of_vault_sync_job SET owner_id=owner_id+1", "DELETE FROM of_vault_sync_job"]:
        with pytest.raises(DBAPIError), database[1].begin() as c: c.exec_driver_sql(statement)
    store.close()


def test_refresh_preserves_human_metadata_and_missing_ready_is_not_sellable(database, monkeypatch):
    store, account, _, _ = sync(database, monkeypatch)
    with Session(database[1]) as s, s.begin():
        row = s.get(VaultItem, (account, 11))
        row.description, row.tags, row.notes = "Human description", '["human-tag"]', "Human notes"
        row.operator_overrides_json, row.locked_fields_json = '{"description":"locked"}', '["description"]'
        row.default_price_cents, row.send_count = 1234, 8
    missing = media(); del missing["isReady"]
    queue(store, account, "e" * 64)
    monkeypatch.setattr(NativeProviderConnector, "vault_page", lambda self, **kw: page([missing]))
    assert process_vault_jobs(store, TENANT) == 1
    result = list_vault(store, TENANT, account)["items"][0]
    assert result["ready"] is False and result["reason"] == "readiness_unknown"
    with Session(database[1]) as s:
        row = s.get(VaultItem, (account, 11))
        assert (row.description, row.tags, row.notes, row.default_price_cents, row.send_count) == (
            "Human description", '["human-tag"]', "Human notes", 1234, 8)
        assert row.operator_overrides_json == '{"description":"locked"}' and row.locked_fields_json == '["description"]'
    with pytest.raises(Refused, match="media unavailable"): create(store, account)
    store.close()


def test_product_uses_canonical_catalogue_exact_ack_and_no_duplicate_assets(database, monkeypatch):
    store, account, _, _ = sync(database, monkeypatch)
    acknowledgement = create(store, account)
    assert acknowledgement["item"]["revision"] == 0 and acknowledgement["item"]["priceCents"] == 1000
    assert create(store, account) == acknowledgement
    item = int(acknowledgement["itemId"])
    update_content(store, TENANT, account, item, patch={"label": "Later human edit"}, expected_revision=0,
        expected_fingerprint=acknowledgement["item"]["fingerprint"], command_key="e" * 64, actor=ACTOR)
    assert create(store, account) == acknowledgement, "historical replay returns original creation ACK"
    assert get_content(store, TENANT, account, item)["label"] == "Later human edit"
    with pytest.raises(Refused): create(store, account, priceCents=1200)
    with pytest.raises(Refused): create(store, account, key="f" * 64)
    with Session(database[1]) as s:
        row = s.get(CatalogItem, item)
        assert (row.account_id, row.kind, row.media_ids, row.preview_media_ids) == (account, "image_set", "[11,12]", "[11]")
        assert s.scalar(select(func.count()).select_from(CatalogItem)) == 1
        assert s.scalar(select(func.count()).select_from(OfVaultProductCreate)) == 1
    with pytest.raises(DBAPIError), database[1].begin() as c: c.exec_driver_sql("DELETE FROM of_vault_product_create")
    store.close()


@pytest.mark.parametrize("patch", [{"mediaIds": []}, {"mediaIds": ["11", "11"]}, {"mediaIds": [11]},
    {"mediaIds": ["01"]}, {"previewMediaIds": ["11", "12"]}, {"previewMediaIds": ["99"]},
    {"description": ""}, {"priceCents": True}, {"priceCents": 3.99}, {"priceCents": 299},
    {"enabled": 1}, {"sourceUrl": "https://caller.example"}])
def test_strict_product_inputs(patch):
    with pytest.raises(InvalidVault): validate_product(product(**patch))


def test_video_previews_foreign_or_unverified_media_and_readiness(database, monkeypatch):
    store, account, _, _ = sync(database, monkeypatch)
    ack = create(store, account, mediaIds=["13", "11"], previewMediaIds=["11"])
    with Session(database[1]) as s:
        row = s.get(CatalogItem, int(ack["itemId"]))
        assert row.kind == "video" and row.duration_sec == 62
    for args in [{"mediaIds": ["11", "13"], "previewMediaIds": ["13"]},
                 {"mediaIds": ["11", "13"], "previewMediaIds": []},
                 {"mediaIds": ["999"], "previewMediaIds": []}]:
        with pytest.raises((InvalidVault, Refused)): create(store, account, key="f" * 64, **args)
    with Session(database[1]) as s, s.begin(): s.get(VaultItem, (account, 12)).raw_json = '{}'
    with pytest.raises(Refused): create(store, account)
    store.close()


def test_partial_invalid_page_is_not_applied_and_provider_errors_are_redacted(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch); queue(store, account)
    monkeypatch.setattr(NativeProviderConnector, "vault_page", lambda self, **kw: page([media(), media(12, isReady="bad")]))
    assert process_vault_jobs(store, TENANT) == 0
    result = list_vault(store, TENANT, account)
    assert result["items"] == [] and result["sync"]["lastError"] == "unsupported_vault_response"
    queue(store, account, "e" * 64)
    def fail(self, **kw): raise RuntimeError("cookie=secret proxy=https://private.example")
    monkeypatch.setattr(NativeProviderConnector, "vault_page", fail)
    assert process_vault_jobs(store, TENANT) == 0
    assert list_vault(store, TENANT, account)["sync"]["lastError"] == "vault_read_failed"
    store.close()


def test_stop_competing_workers_and_generation_replacement_never_apply_old_page(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch); queue(store, account)
    stop = threading.Event(); stop.set()
    calls = []
    def read(self, **kw):
        calls.append(1)
        with Session(database[1]) as s, s.begin(): s.get(OfProviderConnection, account).revision += 1
        return page([media()])
    monkeypatch.setattr(NativeProviderConnector, "vault_page", read)
    assert process_vault_jobs(store, TENANT, stop=stop) == 0 and calls == []
    assert process_vault_jobs(store, TENANT) == 0 and calls == [1]
    result = list_vault(store, TENANT, account)
    assert result["sync"]["status"] == "superseded" and result["items"] == []
    store.close()


def test_api_content_purpose_strict_json_scope_and_no_provider_on_request(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "api")
    monkeypatch.setenv("ONLYFANS_CONTENT_TOKEN", "synthetic-content-only")
    monkeypatch.setenv("ONLYFANS_CONTENT_TENANT", TENANT)
    app = FastAPI(); app.state.control_store = store; app.include_router(router)
    client = TestClient(app)
    base = f"/onlyfans/content/v1/accounts/{account}"
    headers = {"authorization": "Bearer synthetic-content-only", HEADER: SCHEMA}
    assert client.get(base + "/vault").status_code == 401
    assert client.get(base + "/vault", headers=headers).json()["vault"]["items"] == []
    summary = client.get(base + "/vault?includeSummary=1", headers=headers)
    assert summary.status_code == 200 and summary.json()["vault"]["summary"]["total"] == 0
    assert client.get(base + "/vault?includeSummary=yes", headers=headers).status_code == 400
    assert client.get(base + "/vault?includeSummary=1&includeSummary=1", headers=headers).status_code == 400
    assert client.get(base + "/vault?limit=999", headers=headers).status_code == 400
    assert client.get(base + "/vault?limit=1&limit=2", headers=headers).status_code == 400
    body = {"expectedConnectionRevision": 1, "commandKey": "c" * 64, "actor": ACTOR}
    result = client.post(base + "/vault/sync", headers=headers, json=body)
    assert result.status_code == 200 and result.json()["commandKey"] == body["commandKey"]
    assert result.json()["sync"]["status"] == "pending"
    assert set(result.json()["sync"]) == {"jobId","commandKey","status","pagesRead","itemsSeen",
        "lastError","requestedAt","updatedAt","completedAt"}
    rich=client.post(base+"/vault/sync?progress=1",headers=headers,json=body).json()["sync"]
    assert rich.pop("retryAt") is None and rich.pop("pageOffset")==0
    assert rich==result.json()["sync"]
    legacy=client.get(base+"/vault",headers=headers).json()["vault"]["sync"]
    extended=client.get(base+"/vault?progress=1",headers=headers).json()["vault"]["sync"]
    assert extended.pop("retryAt") is None and extended.pop("pageOffset")==0
    assert extended==legacy
    assert client.get(base+"/vault?progress=yes",headers=headers).status_code==400
    assert client.post(base+"/vault/sync?progress=1&progress=1",headers=headers,json=body).status_code==400
    assert client.post(base + "/vault/sync", headers=headers, json=body).json() == result.json()
    with Session(database[1]) as session, session.begin():
        job=session.get(OfVaultSyncJob,(account,result.json()["sync"]["jobId"]))
        job.last_error="provider_read_deferred:provider_rate_limit"
        job.lease_until=utcnow()+timedelta(seconds=30)
    legacy=client.get(base+"/vault",headers=headers).json()["vault"]["sync"]
    extended=client.get(base+"/vault?progress=1",headers=headers).json()["vault"]["sync"]
    assert legacy["lastError"]=="provider_read_deferred"
    assert extended["lastError"]=="provider_read_deferred:provider_rate_limit"
    assert extended["retryAt"] is not None and extended["pageOffset"]==0
    assert client.post(base + "/vault/sync", headers={**headers, "content-type": "application/json"}, content='{"actor":1,"actor":2}').status_code == 400
    assert client.post(base + "/vault/sync", headers={**headers, "content-type": "application/json"}, content=b" " * 65537).status_code == 413
    assert client.post(base + "/items", headers=headers, json={"product": product(), "commandKey": "d" * 64, "actor": ACTOR}).status_code == 404
    with pytest.raises(Refused): process_vault_jobs(store, TENANT)
    with Session(database[1]) as s, s.begin(): s.get(OfCreatorBinding, s.get(OfRuntime, account).binding_id).revoked_at = utcnow()
    assert client.post(base + "/vault/sync", headers=headers, json=body).status_code == 404
    store.close()



def test_summary_counts_full_account_and_filtered_results_before_pagination(database, monkeypatch):
    store, account, _, _ = sync(database, monkeypatch)
    with Session(store.engine) as session, session.begin():
        session.get(VaultItem, (account, 11)).description = "100% coast"
        session.get(VaultItem, (account, 12)).description = "1000 coast"
        from db.models import Account
        session.add(Account(id="other-account")); session.flush()
        session.add(VaultItem(account_id="other-account", media_id=99, kind="video", created_at=utcnow()))
    result = list_vault(store, TENANT, account, limit=1, include_summary=True)
    assert len(result["items"]) == 1 and result["nextCursor"] is not None
    assert result["summary"] == {"total": 3, "photos": 2, "videos": 1, "other": 0, "matching": 3, "folders": 0}
    assert "summary" not in list_vault(store, TENANT, account)
    assert list_vault(store, TENANT, account, after_media_id="12", include_summary=True)["summary"] == result["summary"]
    assert list_vault(store, TENANT, account, query="100%", include_summary=True)["summary"]["matching"] == 1
    assert list_vault(store, TENANT, account, kind="video", include_summary=True)["summary"]["matching"] == 1
    assert list_vault(store, TENANT, account, query="unmatched", include_summary=True)["summary"]["matching"] == 0
    with pytest.raises(Refused):
        list_vault(store, "unrelated-tenant", account, include_summary=True)
    with pytest.raises(InvalidVault):
        list_vault(store, TENANT, account, include_summary="1")
