"""Regression: a broad request must search past an earlier unpaid card."""
import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from db.models import Base, Account, VaultSend
from db.onlyfans_contextual_models import OfContextualOffer
from automations import pack_sender as packs
from automations.content_contract import Contract, Resolution


@pytest.fixture
def shelf(tmp_path, monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "selection.db"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    @asynccontextmanager
    async def session():
        async with factory() as s:
            yield s
    monkeypatch.setattr(packs, "get_session", session)
    monkeypatch.setattr(packs, "_guard", lambda *_: None)
    monkeypatch.setattr(packs, "_plan_ask_from", AsyncMock(side_effect=lambda *args, **kw: list(args[3])))
    async def setup():
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        async with factory() as s:
            s.add(Account(id="of-test", nickname="Synthetic"))
            await s.flush()
            s.add(VaultSend(account_id="of-test", fan_id=42, media_id=101,
                            message_id=501, price_cents=500, was_purchased=False))
            await s.commit()
    asyncio.run(setup())
    yield factory
    asyncio.run(engine.dispose())


def test_screenshot_request_searches_past_unpaid_media(shelf, monkeypatch):
    contract = Contract(is_ask=True, quote="show me something tonight, what do u have for me")
    async def resolve(*args, **kw):
        assert 101 in kw["seen"]
        return Resolution([102, 103], contract)
    monkeypatch.setattr(packs.content_resolver, "resolve", resolve)
    assert asyncio.run(packs.plan_ask("of-test", 42, contract)) == [102, 103]


def test_stale_resolution_is_reselected_not_whole_pack_rejected(shelf, monkeypatch):
    contract = Contract(is_ask=True)
    fresh = AsyncMock(return_value=Resolution([102, 103], contract))
    monkeypatch.setattr(packs.content_resolver, "resolve", fresh)
    result = asyncio.run(packs.plan_ask("of-test", 42, contract,
                                      resolution=Resolution([101, 102], contract)))
    assert result == [102, 103]
    assert fresh.call_count == 1 and fresh.call_args.kwargs["seen"] == {101}


def test_only_explicit_same_item_request_references_the_existing_card(shelf, monkeypatch):
    contract = Contract(is_ask=True, same_item_requested=True, quote="that same set you sent above")
    monkeypatch.setattr(packs.content_resolver, "resolve", AsyncMock(return_value=Resolution([101], contract)))
    result = asyncio.run(packs.plan_ask("of-test", 42, contract))
    assert result.refusal == "item_already_pending" and result.detail == "501"
    assert not packs._plan_ask_from.called


def test_unidentified_same_item_does_not_sell_a_different_item(shelf, monkeypatch):
    contract = Contract(is_ask=True, same_item_requested=True)
    monkeypatch.setattr(packs.content_resolver, "resolve", AsyncMock(return_value=Resolution([102], contract)))
    result = asyncio.run(packs.plan_ask("of-test", 42, contract))
    assert result.refusal == packs.REFUSE_RESOLVER
    assert not packs._plan_ask_from.called


def test_no_fresh_match_does_not_claim_existing_card_or_sell_duplicate(shelf, monkeypatch):
    contract = Contract(is_ask=True)
    monkeypatch.setattr(packs.content_resolver, "resolve", AsyncMock(return_value=Resolution([101], contract)))
    result = asyncio.run(packs.plan_ask("of-test", 42, contract))
    assert result.refusal == packs.REFUSE_RESOLVER
    assert not packs._plan_ask_from.called


def test_origin_before_projection_also_excluded_and_other_fans_are_scoped(shelf):
    async def run():
        async with shelf() as s:
            from datetime import datetime
            s.add(OfContextualOffer(account_id="of-test", fan_id=42, decision_key="a"*64,
                binding_id="test", source_message_id=1, content_key="b"*64,
                snapshot_json=json.dumps({"media_ids":[102,103],"preview_ids":[103]}),
                state="unknown", created_at=datetime.utcnow()))
            await s.commit()
        assert await packs.offered_media("of-test",42) == {101:501,102:None}
        assert await packs.offered_media("of-test",43) == {}
    asyncio.run(run())


def test_curated_picker_filters_previous_paid_card_too(shelf, monkeypatch):
    monkeypatch.setattr(packs,"_live_of_kind",AsyncMock(side_effect=lambda a,m,k:m))
    monkeypatch.setattr(packs,"_ready_only",AsyncMock(side_effect=lambda a,m,c:m))   # readiness has its own tests
    monkeypatch.setattr(packs.content_resolver,"solo_only",AsyncMock(side_effect=lambda a,m:m))
    monkeypatch.setattr(packs,"spend_bounds",AsyncMock(return_value=(1000,3)))
    monkeypatch.setattr(packs,"rank_by_tier",AsyncMock(side_effect=lambda a,m,t:m))
    assert asyncio.run(packs._available("of-test",42,[101,102],company=False)) == [102]
