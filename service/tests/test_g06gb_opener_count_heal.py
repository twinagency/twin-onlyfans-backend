"""G06g-b heal: priced arc openers booked before `_post_job` wrote counts (batch5 F1) sell 1 of N at
the full day price. `heal_armed_arcs` patches the still-pending registry-listed ones at boot.
Synthetic SQLite, no network."""
import asyncio
import importlib
import json
from contextlib import asynccontextmanager
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import vault_arc
from db.models import Account, AccountAiConfig, Base, ScheduledJob

ACC = "of-g06gb"
OLD = {"posts": [{"text": "c", "media_files": [1, 5, 6], "price": 15.0, "preview_media_files": [5]}]}
RECAP = {"posts": [{"text": "c", "media_files": [1, 5, 6], "price": 0}]}


def _run(tmp_path, monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "heal.db"))
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def session():
        async with factory() as s:
            yield s
            await s.commit()
    monkeypatch.setattr(importlib.import_module("db.engine"), "get_session", session)  # db.engine attr is the engine object
    jobs = [("auto_posts", "feed_paid", OLD, "pending"), ("auto_posts", "feed_paid", OLD, "done"),
            ("auto_posts", "feed_free", RECAP, "pending"), ("auto_posts", None, OLD, "pending")]

    async def go():
        async with engine.begin() as c:
            await c.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[
                m.__table__ for m in (Account, AccountAiConfig, ScheduledJob)]))
        async with factory() as s:
            s.add(Account(id=ACC, nickname="Synthetic"))
            await s.flush()
            rows = [ScheduledJob(account_id=ACC, kind=k, payload_json=json.dumps(p), status=st,
                                 run_at=datetime(2026, 12, 1)) for k, _, p, st in jobs]
            s.add_all(rows)
            await s.flush()
            drops = [{"job_id": r.id, "kind": k, "channel": ch}
                     for r, (k, ch, _, _) in zip(rows, jobs) if ch]   # the 4th job is not an arc drop
            s.add(AccountAiConfig(account_id=ACC, vault_ai_config_json=json.dumps(
                {"ppv_week": {"active_arc": {"drops": drops}}})))
            await s.commit()
        await vault_arc.heal_armed_arcs()
        first = await _payloads(factory)
        await vault_arc.heal_armed_arcs()
        return first, await _payloads(factory)
    try:
        return asyncio.run(go())
    finally:
        asyncio.run(engine.dispose())


async def _payloads(factory):
    async with factory() as s:
        rows = (await s.execute(select(ScheduledJob).order_by(ScheduledJob.id))).scalars().all()
        return [json.loads(r.payload_json)["posts"][0] for r in rows]


def test_pending_pre_fix_opener_gets_the_authored_counts_only(tmp_path, monkeypatch):
    (pending, done, recap, foreign), again = _run(tmp_path, monkeypatch)
    assert pending["media_count"] == 3 and pending["preview_media_count"] == 1
    assert done == OLD["posts"][0]          # fired: history, untouched
    assert recap == RECAP["posts"][0]       # free recap keeps its 1-item sample
    assert foreign == OLD["posts"][0]       # not in the arc registry: an operator rotation pool
    assert again == [pending, done, recap, foreign]   # idempotent
