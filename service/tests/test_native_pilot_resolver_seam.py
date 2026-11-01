"""Vault ask lane model seam: content_resolver/content_prompts reach the model through the engine
capability (pinned, counted, vault-mode only); with no capability the legacy path is unchanged.
Synthetic data, fake model, no network."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

COUNT = 0
ACCOUNT, FAN = "900001", 900002


def check(condition, label):
    global COUNT
    assert condition, label
    COUNT += 1


def fake_model(calls):
    """Answers each resolver purpose with valid JSON, recording (purpose, model)."""
    async def chat(**kw):
        calls.append((kw.get("purpose"), kw.get("model")))
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
            data = {}
        return SimpleNamespace(content=json.dumps(data), parsed=data)
    return chat


async def exercise():
    from db.engine import engine, get_session
    from db.models import Base, Account, Fan, Message, VaultItem
    from automations import content_resolver, pack_sender
    import llm_client
    from native_pilot.content import VaultPlan
    from native_pilot.engine import NativeEngine, PilotBoundaryError, MODEL
    from telegram_runtime.capabilities import active_telegram_capabilities

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.utcnow()
    async with get_session() as s:
        s.add(Account(id=ACCOUNT, nickname="Synthetic creator"))
        s.add(Fan(account_id=ACCOUNT, fan_id=FAN))
        s.add(Message(account_id=ACCOUNT, fan_id=FAN, message_id=5001, direction="in",
                      body="send me feet pics", created_at=now - timedelta(seconds=60)))
        for mid, text in ((7001, "bare feet and soles on the bed"), (7002, "close up of her toes and soles"),
                          (7003, "feet up on the couch, soles to camera"), (7004, "toes in the sand, bare feet"),
                          (7005, "soles and arches, close up"), (7006, "barefoot on the stairs, feet crossed"),
                          (7007, "selfie in the mirror, red dress")):
            s.add(VaultItem(account_id=ACCOUNT, media_id=mid, kind="photo", updated_at_of="2026-10-31T00:00:00+00:00", description=text,
                            search_text=text, tags="[]", created_at=now - timedelta(days=1)))

    def forbid(**kw):
        raise AssertionError("legacy llm_client.chat reached inside the runner")

    async def legacy_forbid(**kw):
        forbid(**kw)

    # 1. vault mode, capability active: every resolver call goes through the engine adapter.
    adapter, sent = [], []
    plan = VaultPlan(ACCOUNT, FAN)
    with patch.object(llm_client, "chat", legacy_forbid):
        with NativeEngine(ACCOUNT, FAN, chat=fake_model(adapter), send=lambda *a: sent.append(a), content=plan):
            cap = active_telegram_capabilities()
            check(cap is not None, "capability active")
            check(await content_resolver.resolve_model(ACCOUNT, "content_resolver") == MODEL, "resolver model is the pinned one")
            res = await content_resolver.resolve(ACCOUNT, FAN, count=2)
            check(res.ok and res.media_ids == [7001, 7002], ("resolver result", res))
            purposes = [p for p, _ in adapter]
            check(purposes == ["content_resolver_contract", "content_resolver_match", "content_resolver_verify"],
                  ("calls counted through adapter", purposes))
            check(all(m == MODEL for _, m in adapter), "every call pinned to the engine model")
            adapter.clear()
            d, refused = await pack_sender.plan_on_ask(ACCOUNT, FAN, cfg={
                "pack_send_enabled": True, "pack_on_ask_enabled": True})
            check(d is not None, ("a card is produced for the described vault item", refused))
            check(d.plan.media and set(d.plan.media) <= set(range(7001, 7007)), ("card media from described feet items", d.plan.media))
            check(d.plan.price_cents > 0, ("card is priced", d.plan.price_cents))
            check(adapter and all(p.startswith("content_resolver") or p == "gather_close_answer" for p, _ in adapter),
                  ("ask lane calls via adapter only", adapter))
            check(not sent, "planning sends nothing")
            # purposes outside the lane stay refused
            for bad in ("content_resolver_x", "other"):
                try:
                    await cap.resolve_model(ACCOUNT, bad)
                except PilotBoundaryError:
                    check(True, "foreign purpose refused " + bad)
                else:
                    raise AssertionError("foreign purpose accepted")
            try:
                await cap.chat(model="other/model", messages=[{"role": "user", "content": "x"}],
                               purpose="content_resolver_match", account_id=ACCOUNT, fan_id=FAN)
            except PilotBoundaryError:
                check(True, "wrong model refused")
            else:
                raise AssertionError("wrong model accepted")

    # 2. text-only and catalogue engines (no vault_mode) refuse the resolver purposes at the boundary.
    for content in (None,):
        adapter.clear()
        with patch.object(llm_client, "chat", legacy_forbid):
            with NativeEngine(ACCOUNT, FAN, chat=fake_model(adapter), send=lambda *a: None, content=content) as eng:
                cap = active_telegram_capabilities()
                try:
                    await cap.resolve_model(ACCOUNT, "content_resolver")
                except PilotBoundaryError:
                    check(True, "text-only: resolver model refused")
                else:
                    raise AssertionError("accepted")
                try:
                    await cap.chat(model=MODEL, messages=[{"role": "user", "content": "x"}],
                                   purpose="content_resolver_match", account_id=ACCOUNT, fan_id=FAN)
                except PilotBoundaryError:
                    check(True, "text-only: resolver chat refused")
                else:
                    raise AssertionError("accepted")
                check(not adapter, "no provider call made")

    # 3. no capability: legacy path unchanged (llm_client.chat and resolve_model are used as before).
    check(active_telegram_capabilities() is None, "capability reset after the pilot")
    legacy = []
    fake = fake_model(legacy)
    async def legacy_model(account_id, purpose, override=None):
        return "legacy/model"
    with patch.object(llm_client, "chat", fake), patch("automations.content_resolver._legacy_resolve_model", legacy_model), \
            patch("automations.content_prompts._legacy_resolve_model", legacy_model):
        check(await content_resolver.resolve_model(ACCOUNT, "content_resolver") == "legacy/model", "legacy model resolution")
        res = await content_resolver.resolve(ACCOUNT, FAN, count=2)
        check(res.ok and res.media_ids == [7001, 7002], "legacy resolver result unchanged")
        check([p for p, _ in legacy] == ["content_resolver_contract", "content_resolver_match", "content_resolver_verify"],
              "legacy calls via llm_client.chat")
        check(all(m == "legacy/model" for _, m in legacy), "legacy model used")


def main():
    import dotenv
    dotenv.load_dotenv = lambda *a, **kw: False
    logging.disable(logging.CRITICAL)
    with tempfile.TemporaryDirectory(prefix="native-resolver-seam-") as tmp:
        env = {"DATABASE_URL": "sqlite+aiosqlite:///" + (Path(tmp) / "seam.db").as_posix(), "PYTHON_DOTENV_DISABLED": "1"}
        with patch.dict(os.environ, env, clear=True):
            def deny(*a, **kw):
                raise AssertionError("network forbidden")
            with patch.object(socket.socket, "connect", deny), patch.object(socket.socket, "connect_ex", deny), \
                    patch.object(socket, "create_connection", deny):
                asyncio.run(exercise())
    print(json.dumps({"native_resolver_seam_checks": COUNT}))


if __name__ == "__main__":
    main()
