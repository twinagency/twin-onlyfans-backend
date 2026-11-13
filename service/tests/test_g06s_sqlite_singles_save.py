"""G06s: put_singles on the legacy SQLite relay (Fastt boot never creates of_delivery_policy).

#117's save-time refusal asked the delivery policy unconditionally, so every enabled single over
mirrored media 500'd with `no such table: of_delivery_policy`. No policy table = no FansAPI
selection = native contract; the removed/kind/unusable gates still refuse."""
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, inspect
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from db.models import Account, Base, CatalogItem, CatalogScript, Proxy, VaultItem

ACCOUNT = "acct-g06s"


def run(tmp_path, monkeypatch, vault):
    import scripts_api as api
    path = tmp_path / "fastt.db"
    sync = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(sync, tables=[t.__table__ for t in (Proxy, Account, CatalogScript, VaultItem, CatalogItem)])
    assert not inspect(sync).has_table("of_delivery_policy")   # as on a Fastt boot
    now = datetime.utcnow()
    with Session(sync) as s, s.begin():
        s.add(Account(id=ACCOUNT, nickname="g06s"))
        s.flush()
        for media_id, kind, removed in vault:
            s.add(VaultItem(account_id=ACCOUNT, media_id=media_id, kind=kind, created_at=now,
                            removed_at=now if removed else None, raw_json=json.dumps({"id": media_id, "type": kind})))
    sync.dispose()
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def get_session():
        async with factory() as s, s.begin():
            yield s
    monkeypatch.setattr(api, "get_session", get_session)
    monkeypatch.setattr(api, "assert_account_owned", lambda a: None)
    item = {"kind": "video", "label": "Blue", "description_for_ai": "A blue set.", "media_ids": [11],
            "preview_media_ids": [11], "price_cents": 1200, "enabled": True}

    async def go():
        try:
            return await api.put_singles(api._ItemsBody(account_id=ACCOUNT, items=[item]))
        finally:
            await engine.dispose()
    return asyncio.run(go())


def test_g06s_sqlite_singles_save_over_mirrored_media(tmp_path, monkeypatch):
    assert run(tmp_path, monkeypatch, [(11, "video", False)]) == {"status": "ok", "items": 1}


def test_g06s_sqlite_singles_save_still_refuses_removed_media(tmp_path, monkeypatch):
    with pytest.raises(HTTPException) as e:
        run(tmp_path, monkeypatch, [(11, "video", True)])
    assert e.value.status_code == 422 and "(removed)" in e.value.detail
