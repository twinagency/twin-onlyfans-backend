"""Synthetic real-native-run regression; no server, credentials or live sockets.

Run with the preflight Python 3.13 interpreter. Only model/provider boundaries
and typing delay are fake; native prompts, history, gates and attribution run.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
import os
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

COUNT = 0


def check(condition, label):
    global COUNT
    assert condition, label
    COUNT += 1


async def exercise():
    from sqlalchemy import select, delete
    from db.engine import engine, get_session
    from db.models import (Base, Account, AccountAiConfig, Fan, Message, Employee,
                           ScheduledJob, CatalogItem, ContentOffer)
    from automations import ai_chatter as native
    import llm_client
    from native_pilot.engine import NativeEngine, PilotBoundaryError, MODEL, TEXT_ONLY
    from telegram_runtime.capabilities import active_telegram_capabilities

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    account, fan = "900001", 900002
    now = datetime.utcnow()
    async with get_session() as s:
        s.add(Account(id=account, nickname="Synthetic creator"))
        s.add(Employee(id=1, display_name="Automation", is_active=True))
        s.add_all([Fan(account_id=account, fan_id=n) for n in (fan, fan + 1)])
        s.add_all([Message(account_id=account, fan_id=n, message_id=1000+n,
                           direction="in", body="How was your morning?",
                           created_at=now-timedelta(seconds=60))
                   for n in (fan, fan + 1)])

    calls, sent, events = [], [], []

    async def chat(**kw):
        calls.append(kw)
        check(kw["model"] == MODEL, "pinned model")
        check(kw["account_id"] == account and kw["fan_id"] == fan,
              "model isolated to selected conversation")
        if kw.get("response_format"):
            return SimpleNamespace(content="{}", parsed={})
        return SimpleNamespace(content="Glad you stopped by. How are you?", parsed=None)

    def send(fid, text, kw):
        check(fid == fan and not kw, "provider only plain text to chosen fan")
        sent.append((fid, text))
        return {"id": 90000000+len(sent), "text": text}

    original_chat, original_config = llm_client.chat, native._load_config
    try:
        with NativeEngine(account, fan, chat=chat, send=send, emit=events.append) as pilot:
            cfg = await native._load_config(account)
            check(all(cfg[k] == v for k, v in TEXT_ONLY.items()), "text-only config")
            check(not cfg["payers_only"], "nonpayer admitted normally")
            check(not await native._runtime_bool(account, "typing_indicator", native.load_typing_indicator),
                  "typing socket disabled")
            check(not await native._runtime_bool(account, "cat_stickers", native.load_cat_stickers_flag),
                  "stickers disabled")
            check(not await native._runtime_day(account, None), "no invented day")
            from automations import teaser_select
            check(await native._runtime_teaser(account, "hot", teaser_select.hot_teaser_config) is None,
                  "hot teaser off")
            check(await native._runtime_teaser(account, "conversation", teaser_select.convo_teaser_config) is None,
                  "convo teaser off")
            client = native._runtime_client(account)
            check(not hasattr(client, "post_json") and not hasattr(client, "unsend_message"),
                  "no raw client or unsend surface")
            for bad_fan, text, kw in [
                (fan+1, "hello", {}), (True, "hello", {}),
                (fan, "", {}), (fan, "hello", {"price": 0}),
                (fan, "hello", {"media_files": []}),
                (fan, "hello", {"gif_id": "x"}),
                (fan, "hello", {"reply_to_message_id": 7}),
            ]:
                try:
                    client.send_message(bad_fan, text, **kw)
                except PilotBoundaryError:
                    check(True, "forbidden send refused")
                else:
                    raise AssertionError("forbidden send accepted")
            check(not sent, "all refusals before provider")
            await native._runtime_enqueue(account, "gen_info", payload={"fan_ids": [fan]})
            check(events[-1]["event"] == "native-background-job-suppressed", "jobs suppressed")
            try:
                with NativeEngine(account, fan, chat=chat, send=send):
                    raise AssertionError("nested engine accepted")
            except PilotBoundaryError:
                check(True, "nested engine refused")
            result = await pilot.run(1)
            check(result["replies_sent"] == 1, "real native reply generated")
            check(len(sent) >= 1 and calls, "native model and provider reached")
            async with get_session() as s:
                rows = (await s.execute(select(Message).where(Message.direction == "out"))).scalars().all()
                check(len(rows) == len(sent), "native writes one attribution per provider message")
                check(all(r.account_id == account and r.fan_id == fan and
                          r.automation_kind == "ai_chatter" for r in rows), "native provenance scoped")
                check(await s.get(AccountAiConfig, account) is None, "no persistent native enablement")
                # Advance only the synthetic cooldown, leaving the real gate intact.
                f = await s.get(Fan, (account, fan))
                f.automation_paused_until = datetime.utcnow()-timedelta(seconds=1)
                s.add(Message(account_id=account, fan_id=fan, message_id=99000000,
                              direction="in", body="I am taking a break today.",
                              created_at=datetime.utcnow()+timedelta(seconds=1)))
            before = len(calls)
            second = await pilot.run(2)
            check(second["replies_sent"] == 1, "second native reply")
            joined = str([c["messages"] for c in calls[before:]])
            check("Glad you stopped by" in joined, "second prompt sees delivered reply")
            check("I am taking a break today" in joined, "second prompt sees fresh inbound")
            check(all(fid == fan for fid, _ in sent), "other fan never reached provider")
            check("127.0.0.1:4311" not in joined, "no Twin adapter in prompt")
            async with get_session() as s:
                check((await s.execute(select(ScheduledJob.id))).first() is None,
                      "native reply schedules no background job")
                s.add(CatalogItem(id=1, account_id=account, label="Synthetic item"))
            before = len(calls), len(sent)
            for status in ("open", "unlocked"):
                async with get_session() as s:
                    s.add(ContentOffer(id=1, account_id=account, fan_id=fan+1,
                                       item_id=1, status=status))
                try:
                    await pilot.run(3)
                except PilotBoundaryError as e:
                    check(str(e) == "open-offer-obligation", "obligation refuses whole pilot")
                else:
                    raise AssertionError("open obligation accepted")
                check((len(calls), len(sent)) == before, "obligation refused before model/provider")
                async with get_session() as s:
                    await s.execute(delete(ContentOffer))
            for wrong in ({"account_id": "999", "fan_id": fan, "model": MODEL},
                          {"account_id": account, "fan_id": fan+1, "model": MODEL},
                          {"account_id": account, "fan_id": fan, "model": "other"}):
                try:
                    await native._runtime_chat(**wrong)
                except PilotBoundaryError:
                    check(True, "model scope refused before callback")
                else:
                    raise AssertionError("model scope accepted")
            check((len(calls), len(sent)) == before, "model scope refuses before callback")
        check(llm_client.chat is original_chat, "model owner was never patched")
        check(native._load_config is original_config, "config owner was never patched")
        check(active_telegram_capabilities() is None, "capability context restored")
        async with get_session() as s:
            f = await s.get(Fan, (account, fan))
            f.automation_paused_until = datetime.utcnow()-timedelta(seconds=1)
            s.add(Message(account_id=account, fan_id=fan, message_id=99000001,
                          direction="in", body="What should we talk about next?",
                          created_at=datetime.utcnow()+timedelta(seconds=2)))
        attempts = []
        def failed_send(fid, text, kw):
            attempts.append(fid)
            raise RuntimeError("synthetic-private-transport-detail")
        try:
            with NativeEngine(account, fan, chat=chat, send=failed_send) as pilot:
                await pilot.run(4)
        except PilotBoundaryError as e:
            check(str(e) == "native-send-interrupted", "send failure reaches runner sanitized")
        else:
            raise AssertionError("native swallowed send refusal")
        check(attempts == [fan], "no send retry after failure")
        check(llm_client.chat is original_chat and native._load_config is original_config,
              "owners unchanged after failed send")
        check(active_telegram_capabilities() is None,
              "capability context restored after failed send")
    finally:
        await engine.dispose()


def main():
    # This script never imports repository dotenv data. Environment and imports
    # are process-local and die with the dedicated test interpreter.
    import dotenv
    dotenv.load_dotenv = lambda *a, **kw: False
    logging.disable(logging.CRITICAL)
    with tempfile.TemporaryDirectory(prefix="native-engine-test-") as tmp:
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite+aiosqlite:///" +
                                     (Path(tmp)/"fixture.db").as_posix(),
                                     "PYTHON_DOTENV_DISABLED": "1"}, clear=True):
            async def guarded():
                # Install after asyncio has created its internal socketpair.
                def deny(*a, **kw):
                    raise AssertionError("network forbidden in native engine fixture")
                with patch.object(socket.socket, "connect", deny), \
                     patch.object(socket.socket, "connect_ex", deny), \
                     patch.object(socket, "create_connection", deny):
                    await exercise()
            asyncio.run(guarded())
    print(f"{COUNT} checks; all cases passed")


if __name__ == "__main__":
    main()
