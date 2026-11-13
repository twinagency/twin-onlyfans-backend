"""G06g: a priced auto_posts item (the vault-arc feed-only opener) passes the sale gate at fire time.

`vault_arc._post_job` → auto_posts runner → `create_post`. Base posted the arc day's media unchecked, so a
removed / hidden / errored / not-ready / outside-FansAPI-selection item rode on a priced wall post.
Synthetic SQLite vault (G06d/G06f fixture), fake OF client that only records; no provider calls, no posts.
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
import vault_import_policy
from db.models import Account, Base, Post, VaultItem
from automations import auto_posts, content_resolver

ACC = "of-g06g"
META = {1: {"id": 1, "type": "photo", "isReady": True},
        2: {"id": 2, "type": "photo", "isReady": False},
        3: {"id": 3, "type": "photo", "isReady": True, "hasError": True},
        4: {"id": 4, "type": "photo", "isReady": True, "isHidden": True},
        5: {"id": 5, "type": "video", "isReady": True},              # DRM: ready, no full URL
        6: {"id": 6, "type": "photo"},                               # isReady absent
        7: {"id": 7, "type": "photo", "isReady": True},              # removed_at set below
        8: {"id": 8, "type": "photo", "isReady": True}}              # FansAPI: outside the selection
REMOVED, OUTSIDE = {7}, {8}
SOLD = {"native": {1, 5, 6, 8}, "fansapi": {1}, "fastt_sqlite": {1, 5, 6, 8}}   # OQ-3R: no DRM (5) on FansAPI


def _opener(media, previews, price_cents=1500):
    """The exact payload `vault_arc.plan_jobs` books for a feed-only priced opener."""
    day = {"media_ids": media, "preview_media_ids": previews, "caption": "synthetic caption"}
    payload = vault_arc._post_job(day, price_cents)
    # Pick the whole pool: isolates the gate from the runner's media_count sampling.
    payload["posts"][0].update(media_count=len(media), preview_media_count=max(len(previews), 1))
    return payload


@pytest.fixture(params=["native", "fansapi", "fastt_sqlite"])  # fastt_sqlite: init_db schema, no policy table
def lane(request, tmp_path, monkeypatch):
    from db.onlyfans_delivery_models import OfDeliveryPolicy
    from db.onlyfans_engine_models import OfFansConnection
    from db.onlyfans_models import OfRuntime
    from db.onlyfans_vault_models import OfVaultObservation, OfVaultSyncJob
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "g06g.db"))
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def session():
        async with factory() as s, s.begin():
            yield s
    for mod in (content_resolver, auto_posts):
        monkeypatch.setattr(mod, "get_session", session)
    posts, jobs = [], []

    async def paced(account_id, fn, **kw):
        return fn()

    def create_post(**kw):
        posts.append(kw)
        return {"id": 900 + len(posts)}

    async def free_page(account_id):
        return False

    async def enqueue(account_id, kind, **kw):
        jobs.append((kind, kw))
        return len(jobs)
    monkeypatch.setattr(ax, "of_write_paced", paced)
    monkeypatch.setattr(ax, "enqueue_job", enqueue)
    monkeypatch.setattr(ax, "_make_client", lambda account_id: SimpleNamespace(create_post=create_post))
    monkeypatch.setattr(account_page, "is_paid_page", free_page)
    # Selection evidence (G06b covers its PG derivation): `refusals` asks it on FansAPI accounts only.
    monkeypatch.setattr(vault_import_policy, "outside_selection", lambda s, a, ids: OUTSIDE & set(ids))

    async def setup():
        async with engine.begin() as c:
            sale = () if request.param == "fastt_sqlite" else (
                OfDeliveryPolicy, OfVaultSyncJob, OfVaultObservation, OfRuntime, OfFansConnection)
            await c.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[m.__table__ for m in (
                Account, Post, VaultItem, *sale)]))
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
    asyncio.run(setup())
    yield SimpleNamespace(want=SOLD[request.param], posts=posts, jobs=jobs)
    asyncio.run(engine.dispose())


def test_priced_opener_with_a_refused_item_skips_never_a_reduced_set(lane):
    """Codex/Flash G06g P1: filtering may not shrink the authored set below its media_count at full price."""
    out = asyncio.run(auto_posts.run(ACC, _opener(list(META), [2, 5, 7]), run_id=1))
    assert (out["status"], out["reason"]) == ("skipped", "media_not_sellable") and lane.posts == []
    out = asyncio.run(auto_posts.run(ACC, _opener([1, 2], []), run_id=1))   # the measured [1,2] repro
    assert out["reason"] == "media_not_sellable" and lane.posts == []


def test_priced_opener_posts_a_fully_sellable_set_whole(lane):
    out = asyncio.run(auto_posts.run(ACC, _opener(sorted(lane.want), [2, 5, 7]), run_id=1))
    assert out["status"] == "ok" and len(lane.posts) == 1
    post = lane.posts[0]
    assert set(post["media_files"]) == lane.want
    assert post["previews"] == sorted({5} & lane.want) and set(post["previews"]) <= set(post["media_files"])
    assert post["price"] == 15.0                     # price kept, like G06f: never re-priced


def test_priced_opener_with_nothing_sellable_skips(lane):
    out = asyncio.run(auto_posts.run(ACC, _opener([2, 3, 4, 7], [2]), run_id=1))
    assert out == {"status": "skipped", "reason": "media_not_sellable", "remaining": 0}
    assert lane.posts == []


def test_priced_opener_with_only_free_previews_skips(lane):
    """Every item is also a free teaser: a price on nothing locked, refuse."""
    out = asyncio.run(auto_posts.run(ACC, _opener([5], [5]), run_id=1))
    assert out["reason"] == ("no_sellable_media" if 5 in lane.want else "media_not_sellable") and lane.posts == []


def test_refused_terminal_post_still_books_the_repost_cycle(lane):
    """Codex/Flash G06g P2: a skipped last item must not end the configured resend cycles."""
    payload = _opener([1, 2], [])
    payload.update(resend_count=2, resend_after_hours=24)
    out = asyncio.run(auto_posts.run(ACC, payload, run_id=1))
    assert out["reason"] == "media_not_sellable" and lane.posts == []
    assert [k for k, _ in lane.jobs] == ["auto_posts"] and out["resend_job_id"] == 1
    job = lane.jobs[0][1]["payload"]
    assert job["resend_count"] == 1 and job["_original_posts"] == payload["posts"]


def test_priced_opener_samples_only_sellable_media(lane):
    """Default media_count=1 samples one id: a refused pick must not sink a post that had a sellable one."""
    payload = vault_arc._post_job({"media_ids": [2, 3, 4, 7, 1], "caption": "c"}, 1500)
    for _ in range(8):
        lane.posts.clear()
        asyncio.run(auto_posts.run(ACC, payload, run_id=1))
        assert [p["media_files"] for p in lane.posts] == [[1]]



def test_priced_post_with_a_refused_folder_pick_skips(lane, monkeypatch):
    """Folder pulls are not pre-filtered: a refused final pick skips the post, never a reduced set at full price."""
    monkeypatch.setattr(auto_posts, "pick_media", lambda spec, client: [1, 2] if spec.get("media_files") else [])
    out = asyncio.run(auto_posts.run(ACC, _opener([1], []), run_id=1))
    assert (out["status"], out["reason"]) == ("skipped", "media_not_sellable") and lane.posts == []
