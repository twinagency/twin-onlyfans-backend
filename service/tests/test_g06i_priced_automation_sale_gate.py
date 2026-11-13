"""G06i: priced automation sends pass the sale gate at fire time (`content_resolver.sale_refusal`).

Synthetic SQLite vault, OF client builder stubbed to raise, no provider calls, no sends. Base sent the
funnel paid step, the funnel opener / mass_premade broadcast and a scheduled send at the price with
whatever media was authored, even if it was not ready / hidden by fire time. Whole set or nothing.
"""
import ast
import asyncio
import json
import pathlib
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import audience_include
import audiences
import automation_executor as ax
from db.models import Account, Base, FunnelAccountMedia, ListMember, MassMessageFunnel, VaultItem
from automations import content_resolver, mass_premade, reply_mass_funnel, scheduled_send, send_mass_message

ACC = "of-g06i"
META = {1: {"id": 1, "type": "photo", "isReady": True},
        2: {"id": 2, "type": "photo", "isReady": False},
        3: {"id": 3, "type": "photo", "isReady": True, "isHidden": True}}
AUTOMATIONS = pathlib.Path(__file__).resolve().parents[1] / "automations"
# Priced-send modules gated by `content_resolver.sellable` directly, before this seam existed.
GATED_BEFORE = {"pack_sender": "G06d", "ppv_send": "G06f", "auto_posts": "G06g"}
# Priced sends still ungated, out of G06i scope (follow-up). Remove an entry once it calls the seam.
KNOWN_UNGATED = {"ai_chatter": "catalog shelf PPV sends", "make_right": "ppv_folder pivot"}


class Wire(Exception):
    """Raised where the send would build its OF client: reaching it = the gate let the send through."""


@pytest.fixture
def lane(tmp_path, monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "g06i.db"))
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def session():
        async with factory() as s:
            yield s
    for mod in (content_resolver, send_mass_message):
        monkeypatch.setattr(mod, "get_session", session)
    sent = []

    def wire(account_id):   # both senders build the OF client right after the gate, then write
        sent.append(account_id)
        raise Wire

    async def allowed(*a, **kw):
        return True, ""

    async def audience(account_id, **kw):
        return {"included_users": [101], "excluded_users": []}

    async def no_banned(account_id):
        return [], "mask"
    monkeypatch.setattr(ax, "_make_client", wire)
    monkeypatch.setattr(audience_include, "check_at_fire", allowed)
    monkeypatch.setattr(audiences, "resolve_mass_audience", audience)
    monkeypatch.setattr(send_mass_message, "load_banned_words", no_banned)

    async def setup():
        async with engine.begin() as c:
            await c.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[m.__table__ for m in (
                Account, VaultItem, MassMessageFunnel, FunnelAccountMedia, ListMember)]))
        async with factory() as s:
            s.add(Account(id=ACC, nickname="Synthetic"))
            s.add(MassMessageFunnel(id=3, name="f", opening_message="hey", steps_json="[]"))
            await s.flush()
            for mid, raw in META.items():
                s.add(VaultItem(account_id=ACC, media_id=mid, kind=raw["type"], raw_json=json.dumps(raw),
                                content_hash=f"h{mid}", created_at=datetime(2026, 1, 1, 0, mid)))
            await s.commit()

    async def opener(media):
        async with factory() as s:
            await s.merge(FunnelAccountMedia(funnel_id=3, account_id=ACC, opening_media_ids=json.dumps(media)))
            await s.commit()
    asyncio.run(setup())
    yield SimpleNamespace(sent=sent, opener=lambda m: asyncio.run(opener(m)))
    asyncio.run(engine.dispose())


def _reaches_wire(coro) -> bool:
    try:
        asyncio.run(coro)
    except Wire:
        return True
    return False


# ── scheduled_send ──────────────────────────────────────────────────────

def _scheduled(price, media):
    return scheduled_send.run(ACC, {"fan_id": 101, "text": "hi", "price": price, "media_files": media}, run_id=1)


def test_scheduled_priced_send_with_an_unsellable_item_is_refused(lane):
    assert asyncio.run(_scheduled(10, [1, 2]))["reason"] == "media_not_sellable"
    assert lane.sent == []


def test_scheduled_free_or_fully_sellable_send_still_goes_out(lane):
    assert _reaches_wire(_scheduled(10, [1]))
    assert _reaches_wire(_scheduled(0, [2, 3]))                       # free: not a sale
    assert _reaches_wire(_scheduled(10, [{"claim": "fresh-upload"}]))  # not a vault row


# ── send_mass_message: the funnel opener at the payload price ──────────

def _opener_send(lane, price, media):
    lane.opener(media)
    return send_mass_message.run(ACC, {"funnel_id": 3, "price": price, "included_users": [101]}, run_id=1)


def test_funnel_opener_priced_with_an_unsellable_item_is_refused(lane):
    res = asyncio.run(_opener_send(lane, 15, [1, 3]))
    assert res["reason"] == "media_not_sellable" and res["refused_media"] == 1


def test_funnel_opener_free_or_fully_sellable_still_goes_out(lane):
    assert _reaches_wire(_opener_send(lane, 15, [1]))
    assert _reaches_wire(_opener_send(lane, 0, [2]))


# ── mass_premade (delegates to send_mass_message) ──────────────────────

def _premade(price, media):
    spec = {"text": "hi", "price": price, "media_files": media, "media_count": len(media),
            "included_users": [101]}
    return mass_premade.run(ACC, {"messages": [spec]}, run_id=1)


def test_mass_premade_priced_with_an_unsellable_item_is_refused(lane):
    assert asyncio.run(_premade(20, [1, 2]))["reason"] == "media_not_sellable"


def test_mass_premade_free_or_fully_sellable_still_goes_out(lane):
    assert _reaches_wire(_premade(20, [1]))
    assert _reaches_wire(_premade(0, [3]))


# ── reply_mass_funnel: the paid_ppv step (pre-send guard → blocked, backs off, resumes) ──

def _step_problems(step, media):
    return asyncio.run(reply_mass_funnel._presend_problems(ACC, step, True, ["unlock me"], media))


def test_funnel_paid_step_with_an_unsellable_item_is_blocked(lane):
    assert _step_problems({"type": "paid_ppv", "price": 24}, [1, 2]) == ["media_not_sellable"]
    assert _step_problems({"type": "paid_ppv"}, [3]) == ["media_not_sellable"]   # default price is a sale


def test_funnel_paid_step_fully_sellable_or_free_passes(lane):
    assert _step_problems({"type": "paid_ppv", "price": 24}, [1]) == []
    assert _step_problems({"type": "paid_ppv", "price": 0}, [2]) == []


# ── structural guard: no priced automation send outside the seam ───────

def _priced_send_sites(tree) -> bool:
    """True if a function both sets a non-zero `price` and `media_files` (kwarg, dict key, or
    `kw["media_files"] = …`): the shape of every priced OF send / post built in automations."""
    def zero(v):
        return isinstance(v, ast.Constant) and v.value in (0, 0.0, None)
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        price = media = False
        for n in ast.walk(fn):
            if isinstance(n, ast.keyword):
                price |= n.arg == "price" and not zero(n.value)
                media |= n.arg == "media_files"
            elif isinstance(n, ast.Dict):
                for k, v in zip(n.keys, n.values):
                    if isinstance(k, ast.Constant):
                        price |= k.value == "price" and not zero(v)
                        media |= k.value == "media_files"
            elif isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Store):
                media |= isinstance(n.slice, ast.Constant) and n.slice.value == "media_files"
        if price and media:
            return True
    return False


def _calls(tree, attr) -> bool:
    return any(isinstance(n, ast.Attribute) and n.attr == attr
               and isinstance(n.value, ast.Name) and n.value.id == "content_resolver"
               for n in ast.walk(tree))


def test_every_priced_automation_send_goes_through_the_sale_gate():
    priced, seam = set(), set()
    for p in AUTOMATIONS.glob("*.py"):
        tree = ast.parse(p.read_text())
        if _priced_send_sites(tree):
            priced.add(p.stem)
        if _calls(tree, "sale_refusal"):
            seam.add(p.stem)
    assert {"reply_mass_funnel", "send_mass_message", "scheduled_send"} <= priced & seam
    ungated = priced - seam - set(GATED_BEFORE) - set(KNOWN_UNGATED)
    assert not ungated, f"priced send without content_resolver.sale_refusal: {sorted(ungated)}"
    for mod in GATED_BEFORE:   # allowlist stays honest: still priced, still gated
        tree = ast.parse((AUTOMATIONS / f"{mod}.py").read_text())
        assert mod in priced and (_calls(tree, "sellable") or mod in seam), mod
    assert not (set(KNOWN_UNGATED) & seam), "now gated: drop it from KNOWN_UNGATED"
    assert set(KNOWN_UNGATED) <= priced, "no longer a priced send: drop it from KNOWN_UNGATED"
