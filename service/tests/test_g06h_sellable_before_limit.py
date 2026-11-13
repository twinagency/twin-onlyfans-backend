"""G06h: the sale gate filters BEFORE the LIMIT window, not after it.

Synthetic SQLite vault, no provider calls, no sends. Base took the newest N rows, then dropped refused ones,
so a window full of refused rows came back empty while sellable items sat deeper in the table; the
resolver's broad fallback fired only on an empty RAW match, never on an empty sellable one.
"""
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from db.models import Account, Base, VaultFolder, VaultFolderItem, VaultItem, VaultSend
from automations import content_resolver, pack_sender, sales_turn

ACC = "of-g06h"
REFUSED = 601            # one past the resolver's 600-row window (and the shelf's 400)
DEEP = 10_000            # the one sellable item, older than every refused row
T0 = datetime(2026, 1, 1)


@pytest.fixture(params=["native", "fastt_sqlite"])
def factory(request, tmp_path, monkeypatch):
    from db.onlyfans_delivery_models import OfDeliveryPolicy
    from db.onlyfans_engine_models import OfFansConnection
    from db.onlyfans_models import OfRuntime
    from db.onlyfans_vault_models import OfVaultObservation, OfVaultSyncJob
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "g06h.db"))
    maker = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def session():
        async with maker() as s:
            yield s
    for mod in (content_resolver, pack_sender, sales_turn):
        monkeypatch.setattr(mod, "get_session", session)
    sale = () if request.param == "fastt_sqlite" else (
        OfDeliveryPolicy, OfVaultSyncJob, OfVaultObservation, OfRuntime, OfFansConnection)

    async def make(items):
        async with engine.begin() as c:
            await c.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[m.__table__ for m in (
                Account, VaultItem, VaultFolder, VaultFolderItem, VaultSend, *sale)]))
        async with maker() as s:
            s.add(Account(id=ACC, nickname="Synthetic"))
            await s.flush()
            for mid, ready, text, age in items:
                s.add(VaultItem(account_id=ACC, media_id=mid, kind="photo",
                                raw_json=json.dumps({"id": mid, "type": "photo", "isReady": ready}),
                                description=text, search_text=text, content_hash=f"h{mid}",
                                created_at=T0 - timedelta(seconds=age)))
            await s.commit()
    yield SimpleNamespace(make=lambda items: asyncio.run(make(items)), session=session)
    asyncio.run(engine.dispose())


FEET = "barefoot beach soles feet"


def _deep_vault(f):
    """REFUSED newest not-ready rows, then one sellable row older than all of them."""
    f.make([(i, False, FEET, i) for i in range(1, REFUSED + 1)] + [(DEEP, True, FEET, DEEP)])


def test_described_pool_reaches_past_a_refused_window(factory):
    _deep_vault(factory)
    pool = asyncio.run(content_resolver._described_pool(ACC, set(), None, ["feet"]))
    assert [m for m, _ in pool] == [DEEP]


def test_ready_shelf_reaches_past_a_refused_window(factory):
    _deep_vault(factory)
    lane = SimpleNamespace(account_id=ACC, cfg={})
    assert asyncio.run(sales_turn.ready_shelf(lane, 42, session=factory.session)) == {DEEP}


def test_broad_fallback_fires_on_empty_sellable_match(factory):
    """Every term match is refused: that is a sellable miss, so degrade to the broad pool (the matcher judges)."""
    factory.make([(1, False, FEET, 1), (2, False, FEET, 2), (3, True, "red dress by the window", 3)])
    pool = asyncio.run(content_resolver._described_pool(ACC, set(), None, ["feet"]))
    assert [m for m, _ in pool] == [3]


def test_refused_rows_never_enter_the_pick(factory):
    """Gate stays exactly as strict: paging past refused rows never lets one through."""
    _deep_vault(factory)
    pool = asyncio.run(content_resolver._described_pool(ACC, set(), None, []))
    assert {m for m, _ in pool} == {DEEP}
