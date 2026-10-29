"""Run the actual bounded caption child against disposable PostgreSQL.

Only provider/frame/model boundaries are synthetic. No credential or network
request is used; advisory locking, scope guards, persistence and folders are real.
"""
import asyncio
import hashlib
import json
import os
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from test_onlyfans_foundation_pg import database
from db.models import Account, VaultItem, VaultFolderItem, OfCreatorBinding
from db.onlyfans_models import OfRuntime
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_vault_models import OfVaultObservation, OfVaultSyncJob
from onlyfans_runtime.store import utcnow, Store, Refused
from onlyfans_runtime.native_context import canonical
from onlyfans_runtime.vault_management import digest
from native_pilot.tools import describe_imported_vault as child

ACCOUNT, TENANT, BINDING, MID = "of-900001", "tenant-caption-test", "caption-binding", 11001


def setup(database, monkeypatch):
    url, engine = database
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    monkeypatch.setenv("ONLYFANS_WORKER_TENANT", TENANT)
    monkeypatch.setenv("DATABASE_URL", url)
    import importlib
    db = importlib.import_module("db.engine")
    async_engine = create_async_engine(engine.url.set(drivername="postgresql+asyncpg"), pool_size=1, max_overflow=0)
    monkeypatch.setattr(db, "async_session_factory", async_sessionmaker(async_engine, expire_on_commit=False))
    from onlyfans_runtime import connections
    from native_pilot import connection
    from native_pilot.tools import caption_vault
    import llm_client, vault_frames
    from onlyfans_runtime import vault_previews
    monkeypatch.setattr(connections, "_open", lambda *a: {"capture": {}, "xHash": "synthetic", "proxyUrl": None})
    fake_client = SimpleNamespace(_http_call=lambda *a, **k: None, http=SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(connection, "build_client", lambda conn: fake_client)
    monkeypatch.setattr(caption_vault, "install_client_adapter", lambda *a: None)
    monkeypatch.setattr(caption_vault, "install_llm_adapter", lambda: None)
    monkeypatch.setattr(vault_previews, "_read", lambda *a: b"synthetic-owned-image")
    calls = []
    async def collect(account, item, raw, want):
        return vault_frames.Collected(["data:image/jpeg;base64,AAAA"], "ok")
    async def chat(**kwargs):
        assert kwargs["account_id"] == ACCOUNT and kwargs["purpose"] == "describe_media"
        assert os.environ.get("ONLYFANS_PROCESS_ROLE") == "worker"
        assert llm_client._vault_caption_account.get() == ACCOUNT
        if kwargs.get("temperature") == 0.0:
            assert kwargs["model"] == "openrouter/qwen3-vl-235b", "Use the configured provider and the same account budget"
            data = {"description_accurate": True, "checks": [{"category": "outdoors", "confirmed": True, "confidence": 99, "evidence": "The outdoor surroundings are clearly visible."}]}
        else:
            calls.append((ACCOUNT, MID))
            data = {"description": "Synthetic outdoors portrait", "tags": ["outdoors"], "explicitness": "sfw", "category_evidence": [{"category": "outdoors", "confidence": 99, "evidence": "The outdoor surroundings are clearly visible.", "frames": [1]}]}
        return SimpleNamespace(content=json.dumps(data), call_id=None, cost_cents=1)
    async def cap(*a):
        return {"capped": False, "spent_millicents": 0, "cap_millicents": 10000}
    monkeypatch.setattr(vault_frames, "collect", collect)
    monkeypatch.setattr(llm_client, "chat", chat)
    monkeypatch.setattr(llm_client, "cap_state", cap)
    now = utcnow()
    raw = canonical({"id": MID, "type": "photo", "userId": 900001})
    with Session(engine) as s, s.begin():
        s.add(Account(id=ACCOUNT)); s.flush()
        s.add(OfCreatorBinding(id=BINDING, tenant_id=TENANT, creator_id="creator-caption", creator_name="Synthetic", account_id=ACCOUNT, handle="synthetic", channel="onlyfans")); s.flush()
        s.add(OfRuntime(account_id=ACCOUNT, binding_id=BINDING, enabled=False, activated_at=now)); s.flush()
        from datetime import timedelta
        s.add(OfProviderConnection(account_id=ACCOUNT, binding_id=BINDING, owner_id=900001, revision=1, sealed_config="synthetic", expires_at=now+timedelta(hours=1), verified_at=now, status="verified", updated_at=now))
        s.add(OfVaultSyncJob(account_id=ACCOUNT, job_id="caption-sync", binding_id=BINDING, command_key="c"*64, request_hash="a"*64, actor="worker-import:synthetic", connection_revision=1, owner_id=900001, max_pages=200, status="complete", page_offset=1, pages_read=1, items_seen=1, requested_at=now, updated_at=now)); s.flush()
        s.add(VaultItem(account_id=ACCOUNT, media_id=MID, kind="photo", created_at=now, raw_json=raw)); s.flush()
        s.add(OfVaultObservation(account_id=ACCOUNT, media_id=MID, job_id="caption-sync", raw_hash=digest(raw), observed_at=now))
    return SimpleNamespace(tenant=TENANT, account=ACCOUNT, binding=BINDING, revision=1, limit=1), calls, async_engine, fake_client


def test_real_child_persists_caption_categories_and_restart_skips(database, monkeypatch):
    args, calls, engine, _ = setup(database, monkeypatch)
    async def run():
        try:
            result = await child.run(args)
            assert result["described"] == 1 and result["failed"] == 0
            assert (await child.run(args))["described"] == 0
        finally:
            await engine.dispose()
    asyncio.run(run())
    assert calls == [(ACCOUNT, MID)]
    assert os.environ["ONLYFANS_PROCESS_ROLE"] == "worker"
    with Session(database[1]) as s:
        item = s.get(VaultItem, (ACCOUNT, MID))
        assert item.description == "Synthetic outdoors portrait"
        assert s.scalar(select(VaultFolderItem).where(VaultFolderItem.account_id == ACCOUNT)) is not None
        assert s.get(OfRuntime, ACCOUNT).enabled is False


def test_real_advisory_lock_prevents_duplicate_batch(database, monkeypatch):
    args, calls, engine, _ = setup(database, monkeypatch)
    key = int.from_bytes(hashlib.sha256(("vault-description.v1:"+TENANT+":"+ACCOUNT).encode()).digest()[:8], "big", signed=True)
    with database[1].connect().execution_options(isolation_level="AUTOCOMMIT") as lease:
        lease.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
        async def run():
            try:
                assert (await child.run(args))["state"] == "busy"
            finally:
                await engine.dispose()
        asyncio.run(run())
        lease.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
    assert calls == []


def test_existing_tagged_media_is_rechecked_before_uncaptioned_imports(database, monkeypatch):
    args, calls, engine, _ = setup(database, monkeypatch)
    with Session(database[1]) as s,s.begin():
        item=s.get(VaultItem,(ACCOUNT,MID));item.describe_status="described";item.description="Legacy caption"
        raw=canonical({"id":MID-1,"type":"photo","userId":900001})
        s.add(VaultItem(account_id=ACCOUNT,media_id=MID-1,kind="photo",created_at=utcnow(),raw_json=raw));s.flush()
        s.add(OfVaultObservation(account_id=ACCOUNT,media_id=MID-1,job_id="caption-sync",raw_hash=digest(raw),observed_at=utcnow()))
    async def run():
        try:assert (await child.run(args))["described"] == 1
        finally:await engine.dispose()
    asyncio.run(run())
    with Session(database[1]) as s:
        assert "_folder_evidence_v1" in s.get(VaultItem,(ACCOUNT,MID)).ai_fields_json
        assert s.get(VaultItem,(ACCOUNT,MID-1)).describe_status is None


def test_folder_and_preview_discovery_obey_the_same_creator_lanes(database, monkeypatch):
    args, calls, engine, _ = setup(database, monkeypatch)
    from onlyfans_runtime.vault_folder_worker import accounts
    store=Store(database[0])
    try:
        monkeypatch.setenv("ONLYFANS_VAULT_DESCRIPTION_INCLUDE_ACCOUNTS",ACCOUNT)
        monkeypatch.setenv("ONLYFANS_VAULT_DESCRIPTION_EXCLUDE_ACCOUNTS","")
        assert accounts(store,TENANT)==[ACCOUNT]
        monkeypatch.setenv("ONLYFANS_VAULT_DESCRIPTION_EXCLUDE_ACCOUNTS",ACCOUNT)
        assert accounts(store,TENANT)==[]
        monkeypatch.setenv("ONLYFANS_VAULT_DESCRIPTION_INCLUDE_ACCOUNTS","other")
        monkeypatch.setenv("ONLYFANS_VAULT_DESCRIPTION_EXCLUDE_ACCOUNTS","")
        assert accounts(store,TENANT)==[]
    finally:
        store.close();asyncio.run(engine.dispose())


def test_operator_edit_during_collection_fences_generated_write(database, monkeypatch):
    args, calls, engine, _ = setup(database, monkeypatch)
    import vault_frames
    def read_and_edit(*a, **k):
        with Session(database[1]) as s, s.begin():
            s.get(VaultItem, (ACCOUNT, MID)).description = "Operator edit"
        return b"synthetic-owned-image"
    from onlyfans_runtime import vault_previews
    monkeypatch.setattr(vault_previews, "_read", read_and_edit)
    async def run():
        try:
            with pytest.raises(Refused, match="metadata changed"):
                await child.run(args)
        finally:
            await engine.dispose()
    asyncio.run(run())
    with Session(database[1]) as s:
        assert s.get(VaultItem, (ACCOUNT, MID)).description == "Operator edit"


def test_fresh_urls_do_not_rewrite_native_raw_evidence(database, monkeypatch):
    args, calls, engine, client = setup(database, monkeypatch)
    import vault_stills, vault_frames
    with Session(database[1]) as s, s.begin():
        item=s.get(VaultItem,(ACCOUNT,MID));item.kind="video"
        raw=json.loads(item.raw_json);raw["type"]="video";item.raw_json=canonical(raw)
        s.get(OfVaultObservation,(ACCOUNT,MID)).raw_hash=digest(item.raw_json)
    resolved=[]
    def resolve(*a):
        resolved.append(True)
        return vault_stills._Resolved({"id":MID,"type":"video","userId":900001,"files":{}}, False)
    monkeypatch.setattr(vault_stills, "_resolve_fresh_sync", resolve)
    async def collect(account, item, raw, want):
        fresh = await vault_stills.resolve_fresh(account, item.media_id, client)
        assert fresh.media["id"] == MID
        return vault_frames.Collected(["data:image/jpeg;base64,"+str(i) for i in range(6)], "ok", source="storyboard")
    monkeypatch.setattr(vault_frames, "collect", collect)
    async def run():
        try:
            assert (await child.run(args))["described"] == 1
        finally:
            await engine.dispose()
    asyncio.run(run())
    assert resolved == [True]
    with Session(database[1]) as s:
        assert s.get(OfVaultObservation,(ACCOUNT,MID)).raw_hash == digest(s.get(VaultItem,(ACCOUNT,MID)).raw_json)


def test_parallel_photos_keep_scope_and_persist_once(database, monkeypatch):
    monkeypatch.setenv("ONLYFANS_VAULT_COMPACT_DB", "1")
    args, calls, engine, _ = setup(database, monkeypatch)
    args.limit, args.concurrency = 8, 4
    with Session(database[1]) as s, s.begin():
        for mid in range(MID+1, MID+8):
            raw = canonical({"id": mid, "type": "photo", "userId": 900001})
            s.add(VaultItem(account_id=ACCOUNT, media_id=mid, kind="photo", created_at=utcnow(), raw_json=raw))
            s.flush()
            s.add(OfVaultObservation(account_id=ACCOUNT, media_id=mid, job_id="caption-sync", raw_hash=digest(raw), observed_at=utcnow()))
    import llm_client
    original = llm_client.chat
    active = peak = 0
    async def slow_chat(**kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.05)
            return await original(**kwargs)
        finally:
            active -= 1
    monkeypatch.setattr(llm_client, "chat", slow_chat)
    async def run():
        try:
            assert (await child.run(args))["described"] == 8
            assert (await child.run(args))["described"] == 0
            assert llm_client._vault_caption_account.get() is None
        finally:
            await engine.dispose()
    asyncio.run(run())
    assert 2 <= peak <= 4
    assert len(calls) == 8
    assert os.environ["ONLYFANS_PROCESS_ROLE"] == "worker"
    with Session(database[1]) as s:
        items = list(s.scalars(select(VaultItem).where(VaultItem.account_id == ACCOUNT)))
        assert len(items) == 8
        assert all("_folder_evidence_v1" in item.ai_fields_json for item in items)


def test_caption_permission_is_task_local_and_cannot_send_to_fans(monkeypatch):
    import llm_client
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    def stop_at_provider(model):
        raise LookupError("provider boundary reached")
    monkeypatch.setattr(llm_client, "_resolve", stop_at_provider)
    kwargs = dict(model="openrouter/qwen3-vl-30b", messages=[], purpose="describe_media", account_id=ACCOUNT)
    async def run():
        async def unprivileged():
            with pytest.raises(RuntimeError, match="Legacy model"):
                await llm_client.chat(**kwargs)
        task = asyncio.create_task(unprivileged())
        token = llm_client._vault_caption_account.set(ACCOUNT)
        try:
            with pytest.raises(LookupError, match="provider boundary"):
                await llm_client.chat(**kwargs)
            for change in ({"account_id": "of-900009"}, {"purpose": "fan_reply"}, {"fan_id": 12}, {"model": "grok-4-1-fast-non-reasoning"}):
                with pytest.raises(RuntimeError, match="Legacy model"):
                    await llm_client.chat(**{**kwargs, **change})
            await task
        finally:
            llm_client._vault_caption_account.reset(token)
        await unprivileged()
    asyncio.run(run())
