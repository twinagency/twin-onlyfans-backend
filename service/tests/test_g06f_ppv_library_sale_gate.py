"""G06f: PPV-library / arc sends pass the sale gate at fire time (`content_resolver.sellable`).

Synthetic SQLite vault (fixture from G06d), fake mass sender + fake OF client that only record,
no provider calls, no sends. Base attached every library media id unchecked, so a hidden / errored /
not-ready / removed item rode inside a priced mass PPV and its paired feed post.
"""
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import account_page
import automation_executor as ax
import vault_arc
from db.models import Account, AccountAiConfig, Base, Post, VaultItem
from automations import _language, content_resolver, ppv_send, send_mass_message

ACC = "of-g06f"
META = {1: {"id": 1, "type": "photo", "isReady": True},
        2: {"id": 2, "type": "photo", "isReady": False},
        3: {"id": 3, "type": "photo", "isReady": True, "hasError": True},
        4: {"id": 4, "type": "photo", "isReady": True, "isHidden": True},
        5: {"id": 5, "type": "video", "isReady": True},              # DRM: ready, no full URL
        6: {"id": 6, "type": "photo"},                               # isReady absent
        7: {"id": 7, "type": "photo", "isReady": True}}              # removed_at set below
REMOVED = {7}
SOLD = {"native": {1, 5, 6}, "fansapi": {1, 5}, "fastt_sqlite": {1, 5, 6}}  # FansAPI requires isReady true


def _entry(media, previews, **kw):
    """An arc day's library entry: the exact dict `vault_arc` writes for `ppv_send` to load."""
    day = {"media_ids": media, "preview_media_ids": previews, "price_cents": 1500,
           "caption": "synthetic caption", "weekday": "Mon", "theme": "t"}
    return {**vault_arc._library_entry("a1", 1, day, also_feed=True), **kw}


@pytest.fixture(params=["native", "fansapi", "fastt_sqlite"])  # fastt_sqlite: init_db schema, no policy table
def lane(request, tmp_path, monkeypatch):
    from db.onlyfans_delivery_models import OfDeliveryPolicy
    from db.onlyfans_engine_models import OfFansConnection
    from db.onlyfans_models import OfRuntime
    from db.onlyfans_vault_models import OfVaultObservation, OfVaultSyncJob
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "g06f.db"))
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def session():
        async with factory() as s:
            yield s
    for mod in (content_resolver, ppv_send):
        monkeypatch.setattr(mod, "get_session", session)
    sent, posts = [], []

    async def fake_mass(account_id, payload, *, run_id):
        sent.append(payload)
        return {"status": "ok", "recipients": len(payload.get("included_users") or [])}

    async def paced(account_id, fn, **kw):
        return fn()

    def create_post(**kw):
        posts.append(kw)
        return {"id": 900 + len(posts)}

    async def nobody_dark(account_id, fan_ids, now):
        return set()

    async def nobody_owns(account_id, media, rows):
        return list(rows), []

    async def fans(account_id, *, spend_cap=None):
        return ppv_send.EligibleFans([(101, 0), (102, 5000)], {}, 0)

    async def free_page(account_id):
        return False

    async def en(account_id):
        return "en"

    async def no_voice(account_id):
        return SimpleNamespace(voice=None)
    monkeypatch.setattr(send_mass_message, "run", fake_mass)
    monkeypatch.setattr(ax, "of_write_paced", paced)
    monkeypatch.setattr(ax, "_make_client", lambda account_id: SimpleNamespace(create_post=create_post))
    monkeypatch.setattr(account_page, "is_paid_page", free_page)
    monkeypatch.setattr(_language, "load_account_language", en)
    monkeypatch.setattr(ppv_send, "load_voice_blocks", no_voice)
    monkeypatch.setattr(ppv_send, "_eligible_fans", fans)
    monkeypatch.setattr(ppv_send, "_ghost_dark_ids", nobody_dark)
    monkeypatch.setattr(ppv_send, "_split_owners", nobody_owns)

    async def setup():
        async with engine.begin() as c:
            sale = () if request.param == "fastt_sqlite" else (
                OfDeliveryPolicy, OfVaultSyncJob, OfVaultObservation, OfRuntime, OfFansConnection)
            await c.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[m.__table__ for m in (
                Account, AccountAiConfig, Post, VaultItem, *sale)]))
        async with factory() as s:
            s.add(Account(id=ACC, nickname="Synthetic"))
            await s.flush()
            if request.param == "fansapi":
                s.add(OfDeliveryPolicy(account_id=ACC, transport_kind="fansapi"))
            for mid, raw in META.items():
                s.add(VaultItem(account_id=ACC, media_id=mid, kind=raw["type"], raw_json=json.dumps(raw),
                                content_hash=f"h{mid}", created_at=datetime(2026, 1, 1, 0, mid),
                                removed_at=datetime(2026, 2, 1) if mid in REMOVED else None))
            await s.commit()

    async def library(*entries, **extra):
        cfg = {"enabled": True, "ppv_caps": {}, "min_send_gap_minutes": 0, "ppvs": list(entries), **extra}
        async with factory() as s:
            s.add(AccountAiConfig(account_id=ACC, utc_offset=0, ppv_library_config_json=json.dumps(cfg)))
            await s.commit()
    asyncio.run(setup())
    yield SimpleNamespace(want=SOLD[request.param], sent=sent, posts=posts, library=library)
    asyncio.run(engine.dispose())


def _fire(lane, entry):
    asyncio.run(lane.library(entry))
    return asyncio.run(ppv_send.run(ACC, {"ppv_id": entry["id"]}, run_id=1))


def test_arc_send_drops_refused_media(lane):
    """Refused paid ids leave every cell and the paired feed post; previews stay ⊆ sold media."""
    res = _fire(lane, _entry(list(META), [2, 5]))
    assert res["status"] == "ok" and lane.sent
    for p in lane.sent:
        assert set(p["media_files"]) == lane.want
        assert set(p["previews"]) <= set(p["media_files"]) and set(p["previews"]) <= {5}
    assert len(lane.posts) == 1 and set(lane.posts[0]["media_files"]) == lane.want
    assert set(lane.posts[0]["previews"]) <= {5}


def test_nothing_sellable_refuses_the_send(lane):
    """All media refused → skipped with a reason, never a price on zero media (base: sent [2,3,4,7])."""
    res = _fire(lane, _entry([2, 3, 4, 7], [2]))
    assert (res["status"], res["reason"]) == ("skipped", "no_sellable_media")
    assert lane.sent == [] and lane.posts == []


def test_feed_post_refuses_unsellable_media(lane):
    """The manual post-now path (`post_to_feed`) shares the gate."""
    entry = _entry([1, 2, 4], [2])
    res = asyncio.run(ppv_send.post_to_feed(ACC, entry, media_files=[2, 4]))
    assert (res["status"], res["reason"]) == ("skipped", "no_sellable_media") and lane.posts == []
    res = asyncio.run(ppv_send.post_to_feed(ACC, entry))
    assert res["status"] == "ok" and lane.posts[-1]["media_files"] == [1]
    assert lane.posts[-1]["previews"] == []


def test_owner_leg_refuses_unsellable_substitute(lane, monkeypatch):
    """The owner leg's substitute PPV passes the same gate: all refused → no `#sub` send."""
    sub = _entry([2, 3], [], id="sub")

    async def first_owns(account_id, media, rows):
        return list(rows[1:]), list(rows[:1])

    async def substitute(account_id, cfg, now, *, exclude_id):
        return sub
    monkeypatch.setattr(ppv_send, "_split_owners", first_owns)
    monkeypatch.setattr(ppv_send, "_pick_second_ppv", substitute)
    main = _entry(list(META), [], also_post_to_feed=False)
    asyncio.run(lane.library(main, sub, owner_second_leg=True))
    res = asyncio.run(ppv_send.run(ACC, {"ppv_id": main["id"]}, run_id=1))
    assert res["second_leg"]["reason"] == "no_sellable_media"
    assert [p["automation_ref"] for p in lane.sent] == [main["id"]]
