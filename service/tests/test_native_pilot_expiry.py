"""FASTT's own unlock watcher / stall-TTL expiry runs inside the pilot for ONE fan, and sends nothing.

Stock expiry is ``stall_ttl_hours`` (default 6) in ``ai_chatter._resolve_open_offers``; it does not
depend on ``cadence_enabled`` (``offer_expiry_minutes`` is only the cadence leash tier). Synthetic data,
fake model, blocked sockets."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

COUNT = 0
ACCOUNT, FAN, OTHER_FAN = "900001", 900002, 900003


def check(condition, label):
    global COUNT
    assert condition, label
    COUNT += 1


def fake_model(calls):
    async def chat(**kw):
        calls.append(kw.get("purpose"))
        p = kw.get("purpose")
        if p == "content_resolver_contract":
            data = {"is_ask": True, "subject": "feet", "terms": ["feet", "soles"], "media_kind": "photo",
                    "strict": False, "company": False, "custom_request": False, "quote": "send me feet pics"}
        elif p in ("content_resolver_match", "content_resolver_nearest"):
            data = {"media_ids": [7001, 7002, 7003, 7004, 7005, 7006]}
        elif p == "content_resolver_verify":
            data = {"verdicts": {str(n): True for n in range(7001, 7007)}}
        elif p == "content_resolver_action":
            data = {"phrase": "me showing my soles"}
        elif p == "gather_close_answer":
            data = {"line": "you like those huh"}
        else:
            return SimpleNamespace(content="haha ok", parsed=None)
        return SimpleNamespace(content=json.dumps(data), parsed=data)
    return chat


def plan_db(path):
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE vault_items(account_id TEXT,media_id INTEGER,kind TEXT,removed_at TEXT,content_hash TEXT,updated_at_of TEXT,description TEXT,video_description TEXT,ai_fields_json TEXT,operator_overrides_json TEXT,locked_fields_json TEXT)")
        con.execute("CREATE TABLE catalog_items(id INTEGER,account_id TEXT,tags TEXT,enabled INTEGER)")
        con.execute("CREATE TABLE account_ai_config(account_id TEXT,ai_chatter_config_json TEXT)")
        con.executemany("INSERT INTO vault_items(account_id,media_id,kind,removed_at) VALUES(?,?,?,?)",
                        [(ACCOUNT, m, "photo", None) for m in range(7001, 7008)])
        con.executemany("INSERT INTO catalog_items VALUES(?,?,?,?)",
                        [(1, ACCOUNT, '["rung:vault-ask"]', 0), (2, ACCOUNT, '["rung:vault-ask"]', 0)])


async def exercise(tmp):
    from sqlalchemy import select
    from db.engine import engine, get_session
    from db.models import Base, Account, AccountAiConfig, CatalogItem, ContentOffer, Employee, Fan, Message, VaultItem
    from native_pilot.content import VaultPlan
    from native_pilot.engine import NativeEngine, PilotBoundaryError, _ResolverClient, _ResolverRefused
    from automations import ai_chatter
    from telegram_runtime.capabilities import active_telegram_capabilities

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = Path(tmp) / "plan.db"
    plan_db(db)
    now = datetime.utcnow()
    async with get_session() as s:
        s.add(Account(id=ACCOUNT, nickname="Synthetic"))
        s.add(AccountAiConfig(account_id=ACCOUNT, ai_chatter_config_json=json.dumps({"pack_send_enabled":True,"pack_on_ask_enabled":True,"qualification_gate_enabled":True})))
        s.add(Employee(id=1, display_name="Automation", is_active=True))
        s.add_all([Fan(account_id=ACCOUNT, fan_id=FAN), Fan(account_id=ACCOUNT, fan_id=OTHER_FAN)])
        for n in (1, 2):
            s.add(CatalogItem(id=n, account_id=ACCOUNT, label=f"lane {n}", kind="photo", description_for_ai="x",
                              media_ids=json.dumps([7000 + n]), preview_media_ids="[]", duration_sec=0,
                              price_cents=300, is_free_teaser=False, enabled=False, pricing_mode="fixed"))
        for mid in range(7001, 7008):
            text = "bare feet and soles" if mid < 7007 else "selfie in the mirror"
            s.add(VaultItem(account_id=ACCOUNT, media_id=mid, kind="photo", updated_at_of="2026-10-31T00:00:00+00:00", description=text, search_text=text,
                            tags="[]", created_at=now - timedelta(days=1)))

    async def reset_offers(ages):
        """ages: [(fan, item, age_timedelta, offer_message_id|None)] -> fresh content_offers rows."""
        async with get_session() as s:
            for o in (await s.execute(select(ContentOffer))).scalars():
                await s.delete(o)
            for fan, item, age, mid in ages:
                s.add(ContentOffer(account_id=ACCOUNT, fan_id=fan, item_id=item, mode="ppv", price_cents=300,
                                   status="open", offer_message_id=mid, offered_at=now - age, updated_at=now - age))

    async def statuses():
        async with get_session() as s:
            return [(o.fan_id, o.item_id, o.status) for o in
                    (await s.execute(select(ContentOffer).order_by(ContentOffer.id))).scalars()]

    # 0. The resolver's client cannot touch the platform at all.
    rc = _ResolverClient()
    for name, args in (("send_message", (1, "x")), ("unsend_message", (1, 2)), ("get_messages", (1,))):
        try:
            getattr(rc, name)(*args)
        except _ResolverRefused:
            check(True, "resolver client refuses " + name)
        else:
            raise AssertionError(name)
    check(not hasattr(rc, "mark_chat_read") and not hasattr(rc, "delete_message"), "no other platform methods")

    sent, events, calls = [], [], []

    def send(fid, text, kw):
        sent.append((fid, text, kw))
        raise AssertionError("the resolver must never send")

    plan = VaultPlan(ACCOUNT, FAN, str(db))
    unsent = []

    # 1. Directly through the capability: stale card expires, fresh card untouched, other fan untouched,
    #    nothing sent, no unsend (even with unsend_expired_offer true in the account cfg).
    await reset_offers([(FAN, 1, timedelta(hours=26), 5001), (FAN, 2, timedelta(minutes=30), 5002),
                        (OTHER_FAN, 1, timedelta(hours=26), 5003)])
    with NativeEngine(ACCOUNT, FAN, chat=fake_model(calls), send=send, emit=events.append, content=plan,
                      own_offers_only=True):
        cap = active_telegram_capabilities()
        cfg = await cap.load_config(ACCOUNT, ai_chatter._DEFAULTS)
        check(cfg["cadence_enabled"] is False, "cadence stays off")
        check(cfg["stall_ttl_hours"] == 6 and cfg["unsend_expired_offer"] is True, "stock ttl, stock unsend flag in cfg")
        with patch.object(ai_chatter, "_expire_open_offer", wraps=ai_chatter._expire_open_offer) as expire:
            stats = await cap.resolve_open_offers(ACCOUNT, None, cfg, dry_run=False, only_fan_ids={OTHER_FAN})
            check(expire.call_count == 1 and expire.call_args.args[3]["unsend_expired_offer"] is False,
                  "expiry ran with the unsend switch forced off")
        check(stats["offers_expired"] == 1 and stats["offers_unsent"] == 0, ("stats", stats))
        check(await statuses() == [(FAN, 1, "expired"), (FAN, 2, "open"), (OTHER_FAN, 1, "open")],
              ("only this fan's stale offer expired", await statuses()))
        check(not sent, "no send")
        check([e for e in events if e["event"] == "offers-resolved"] == [
            {"event": "offers-resolved", "offers_expired": 1}], ("event", events))
        # idempotent
        stats = await cap.resolve_open_offers(ACCOUNT, None, cfg, dry_run=False)
        check(stats["offers_expired"] == 0, "second pass expires nothing")
        # dry run writes nothing
        await reset_offers([(FAN, 1, timedelta(hours=26), 5001)])
        stats = await cap.resolve_open_offers(ACCOUNT, None, cfg, dry_run=True)
        check(stats["offers_expired"] == 1 and await statuses() == [(FAN, 1, "open")], "dry run counts, writes nothing")

    # 2. A paid card (ledger says paid) is detected and closed without any delivery reaction being sent.
    await reset_offers([(FAN, 1, timedelta(minutes=10), 5001)])
    async with get_session() as s:
        s.add(Message(account_id=ACCOUNT, fan_id=FAN, message_id=5001, direction="out", body="card", price_cents=300,
                      is_paid=True, purchased_at=now, created_at=now - timedelta(minutes=10)))
    with NativeEngine(ACCOUNT, FAN, chat=fake_model(calls), send=send, emit=events.append, content=plan):
        cap = active_telegram_capabilities()
        cfg = await cap.load_config(ACCOUNT, ai_chatter._DEFAULTS)
        stats = await cap.resolve_open_offers(ACCOUNT, None, cfg, dry_run=False)
    check(stats["unlocked_ppv"] == 1 and not sent, ("unlock detected, reaction not sent", stats))
    async with get_session() as s:
        o = (await s.execute(select(ContentOffer))).scalar_one()
    check(o.status == "delivered" and o.resolved_by == "ppv_ledger" and o.delivery_message_id is None,
          ("paid offer resolved without a delivery message", o.status, o.resolved_by))

    # 3. Foreign account is still refused; a non-approved open offer is still an obligation.
    with NativeEngine(ACCOUNT, FAN, chat=fake_model(calls), send=send, content=plan):
        cap = active_telegram_capabilities()
        try:
            await cap.resolve_open_offers("123", None, {}, dry_run=False)
        except PilotBoundaryError as exc:
            check(str(exc) == "wrong-account", "wrong account refused")
        else:
            raise AssertionError("accepted")
    async with get_session() as s:
        s.add(CatalogItem(id=9, account_id=ACCOUNT, label="other", kind="photo", description_for_ai="x",
                          media_ids="[1]", preview_media_ids="[]", duration_sec=0, price_cents=300,
                          is_free_teaser=False, enabled=False, pricing_mode="fixed"))
        s.add(ContentOffer(account_id=ACCOUNT, fan_id=FAN, item_id=9, mode="ppv", price_cents=300, status="open"))
    with NativeEngine(ACCOUNT, FAN, chat=fake_model(calls), send=send, content=plan):
        cap = active_telegram_capabilities()
        try:
            await cap.resolve_open_offers(ACCOUNT, None, {}, dry_run=False)
        except PilotBoundaryError as exc:
            check(str(exc) == "open-offer-obligation", "unapproved open offer is still an obligation")
        else:
            raise AssertionError("accepted")
    async with get_session() as s:
        for o in (await s.execute(select(ContentOffer).where(ContentOffer.item_id == 9))).scalars():
            await s.delete(o)

    # 4. End to end: two unpaid cards from yesterday, a content ask -> they expire first, then the ask lane
    #    produces a NEW vault card (before this, FASTT kept pointing at the old cards / hit the open cap).
    async def turn(offers):
        await reset_offers(offers)
        async with get_session() as s:
            for m in (await s.execute(select(Message).where(Message.fan_id == FAN))).scalars():
                await s.delete(m)
            f = await s.get(Fan, (ACCOUNT, FAN))
            f.automation_paused_until = None
            # He has been chatting since yesterday's cards (stock offer pacing counts fan messages).
            for i, text in enumerate(("hey", "you there?", "thinking of you")):
                s.add(Message(account_id=ACCOUNT, fan_id=FAN, message_id=6100 + i, direction="in", body=text,
                              created_at=datetime.utcnow() - timedelta(minutes=30 - i)))
            s.add(Message(account_id=ACCOUNT, fan_id=FAN, message_id=6001 + len(sent), direction="in",
                          body="send me feet pics", created_at=datetime.utcnow() - timedelta(seconds=60)))
        cards = []

        def send2(fid, text, kw):
            term = plan.terms(kw)
            cards.append((text, kw))
            result = {"id": 8000 + len(cards), "text": text, "createdAt": datetime.utcnow().isoformat(),
                      "fromUser": {"id": 900001}}
            if term:
                result.update(media=[{"id": m, "type": "photo"} for m in kw["media_files"]],
                              mediaCount=len(kw["media_files"]), previews=kw.get("previews", []),
                              price=term["price_cents"] / 100, lockedText=False)
            return result
        with NativeEngine(ACCOUNT, FAN, chat=fake_model(calls), send=send2, emit=events.append, content=plan) as eng:
            stats = await eng.run(6001 + len(sent))
        return stats, cards

    stale = [(FAN, 1, timedelta(hours=26), None), (FAN, 2, timedelta(hours=25), None)]
    stats, cards = await turn(stale)
    st = await statuses()
    check(st[:2] == [(FAN, 1, "expired"), (FAN, 2, "expired")], ("both stale cards expired", st))
    priced = [(t, k) for t, k in cards if k]
    check(len(priced) == 1 and set(priced[0][1]["media_files"]) <= set(range(7001, 7007)),
          ("a new vault card was produced", cards))
    check(len(st) == 3 and st[2][2] == "open", ("the new card is the only open offer", st))

    # Fresh cards remain open. The approved different-item cap removal means
    # they no longer impose a fan-wide two-card ceiling on a fresh request.
    fresh = [(FAN, 1, timedelta(minutes=20), None), (FAN, 2, timedelta(minutes=10), None)]
    stats, cards = await turn(fresh)
    st = await statuses()
    check(st[:2] == [(FAN, 1, "open"), (FAN, 2, "open")], ("fresh cards untouched", st))
    check(len(st) == 3 and len([k for _, k in cards if k]) == 1,
          "a different requested vault item remains eligible")
    await engine.dispose()


def main():
    import dotenv
    dotenv.load_dotenv = lambda *a, **kw: False
    logging.disable(logging.CRITICAL)
    with tempfile.TemporaryDirectory(prefix="native-expiry-") as tmp:
        env = {"DATABASE_URL": "sqlite+aiosqlite:///" + (Path(tmp) / "fixture.db").as_posix(),
               "PYTHON_DOTENV_DISABLED": "1"}
        with patch.dict(os.environ, env, clear=True):
            def deny(*a, **kw):
                raise AssertionError("network forbidden")
            async def run():
                with patch.object(socket.socket, "connect", deny), patch.object(socket.socket, "connect_ex", deny), \
                        patch.object(socket, "create_connection", deny):
                    await exercise(tmp)
            asyncio.run(run())
    print(f"{COUNT} checks; all cases passed")


if __name__ == "__main__":
    main()
