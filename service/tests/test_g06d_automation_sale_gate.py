"""G06d: automation priced-send picks drop media the sale boundary refuses (media_delivery.refusal).

Synthetic SQLite vault, no provider calls, no sends. Base filtered on removed_at only, so a provider
not-ready / errored / hidden item was picked. A ready DRM video (no file URL) stays sellable (OQ-3).
"""
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from db.models import Account, Base, VaultFolder, VaultFolderItem, VaultItem, VaultSend
from automations import content_resolver, pack_audit, pack_sender, sales_turn

ACC = "of-g06d"
META = {1: {"id": 1, "type": "photo", "isReady": True},
        2: {"id": 2, "type": "photo", "isReady": False},
        3: {"id": 3, "type": "photo", "isReady": True, "hasError": True},
        4: {"id": 4, "type": "photo", "isReady": True, "isHidden": True},
        5: {"id": 5, "type": "video", "isReady": True},              # DRM: ready, no full URL
        6: {"id": 6, "type": "photo"},                               # isReady absent
        7: {"id": 7, "type": "photo", "isReady": True}}              # removed_at set below
REMOVED = {7}
SOLD = {"native": {1, 5, 6}, "fansapi": {1, 5}, "fastt_sqlite": {1, 5, 6}}  # FansAPI requires isReady true


@pytest.fixture(params=["native", "fansapi", "fastt_sqlite"])  # fastt_sqlite: init_db schema, no policy table
def vault(request, tmp_path, monkeypatch):
    # Imported here, not at module level: collecting them registers PostgreSQL-only tables in the
    # shared metadata and breaks other files' SQLite create_all in the same pytest process.
    from db.onlyfans_delivery_models import OfDeliveryPolicy
    from db.onlyfans_engine_models import OfFansConnection
    from db.onlyfans_models import OfRuntime
    from db.onlyfans_vault_models import OfVaultObservation, OfVaultSyncJob
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "g06d.db"))
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def session():
        async with factory() as s:
            yield s
    for mod in (content_resolver, pack_audit, pack_sender, sales_turn):
        monkeypatch.setattr(mod, "get_session", session)

    async def setup():
        async with engine.begin() as c:
            sale = () if request.param == "fastt_sqlite" else (   # refusal's FansAPI policy + selection reads
                OfDeliveryPolicy, OfVaultSyncJob, OfVaultObservation, OfRuntime, OfFansConnection)
            await c.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[m.__table__ for m in (
                Account, VaultItem, VaultFolder, VaultFolderItem, VaultSend, *sale)]))
        async with factory() as s:
            s.add(Account(id=ACC, nickname="Synthetic"))
            await s.flush()
            if request.param == "fansapi":
                s.add(OfDeliveryPolicy(account_id=ACC, transport_kind="fansapi"))
            folder = VaultFolder(account_id=ACC, name="feet-nude")
            s.add(folder)
            await s.flush()
            for mid, raw in META.items():
                s.add(VaultItem(account_id=ACC, media_id=mid, kind=raw["type"], raw_json=json.dumps(raw),
                                description="Barefoot on the beach, soles to the camera, warm light.",
                                search_text="barefoot beach soles feet", content_hash=f"h{mid}",
                                created_at=datetime(2026, 1, 1, 0, mid),
                                removed_at=datetime(2026, 2, 1) if mid in REMOVED else None))
                s.add(VaultFolderItem(account_id=ACC, folder_id=folder.id, media_id=mid))
            await s.commit()
    asyncio.run(setup())
    yield SimpleNamespace(want=SOLD[request.param], session=session)
    asyncio.run(engine.dispose())


def test_pack_pick_drops_unusable_media(vault):
    assert set(asyncio.run(pack_sender._live_of_kind(ACC, list(META), None))) == vault.want
    assert asyncio.run(pack_sender._live_of_kind(ACC, list(META), "video")) == [5]   # DRM video control


def test_audit_veto_refuses_unusable_media(vault):
    assert asyncio.run(pack_audit._live_media(ACC, list(META))) == vault.want


def test_contextual_ready_shelf_drops_unusable_media(vault):
    lane = SimpleNamespace(account_id=ACC, cfg={})
    assert asyncio.run(sales_turn.ready_shelf(lane, 42, session=vault.session)) == vault.want


def test_resolver_pools_drop_unusable_media(vault):
    curated = asyncio.run(content_resolver._curated_pool(ACC, "feet", "nude", set()))
    described = asyncio.run(content_resolver._described_pool(ACC, set(), None, ["feet"]))
    assert {m for m, _ in curated} == {m for m, _ in described} == vault.want


def test_pack_previews_drop_unusable_media(vault):
    """Free teaser frames ride inside the priced send: refused ones are dropped, paid ones skipped."""
    tease = list(META)
    assert asyncio.run(pack_sender._live_previews(ACC, tease, [1])) == sorted(vault.want - {1})[:pack_sender.PREVIEW_MAX]


def test_audit_veto_covers_previews(vault):
    """Rule 5 vetoes a refused id attached as a free preview, not only paid ids (base: paid only)."""
    bad = asyncio.run(pack_audit.audit_pack(ACC, "feet", "nude", [1, 5, 2, 7], [2, 7]))
    assert [b for b in bad if b.startswith("rule5")] == ["rule5 media the sale would not sell: [2, 7]"]
