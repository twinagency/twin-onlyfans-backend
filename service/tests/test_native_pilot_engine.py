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

        # Account runners may continue another fan's conversation, while each fan's
        # own unreviewed offer obligations still block its turn.
        plan = SimpleNamespace(catalog_ids={1, 2})
        multi_engine = NativeEngine(account, fan, chat=chat, send=send,
                                    content=plan, own_offers_only=True)
        async with get_session() as session:
            session.add_all([CatalogItem(id=n, account_id=account, label="Synthetic reviewed item")
                             for n in (2, 3)])
            session.add(ContentOffer(id=10, account_id=account, fan_id=fan+1,
                                     item_id=3, mode="ppv", status="open"))
            session.add(ContentOffer(id=11, account_id=account, fan_id=fan,
                                     item_id=2, mode="ppv", status="open"))
        await multi_engine._no_obligations()
        check(True, "all-fans turn ignores another fan and accepts the second reviewed PPV")
        async with get_session() as session:
            session.add(ContentOffer(id=12, account_id=account, fan_id=fan,
                                     item_id=3, mode="ppv", status="open"))
        try:
            await multi_engine._no_obligations()
        except PilotBoundaryError as exc:
            check(str(exc) == "open-offer-obligation", "own unreviewed item remains an obligation")
        else:
            raise AssertionError("unreviewed own offer accepted")
        async with get_session() as session:
            await session.execute(delete(ContentOffer).where(ContentOffer.id == 12))
            offer = await session.get(ContentOffer, 11)
            offer.mode = "tip"
        try:
            await multi_engine._no_obligations()
        except PilotBoundaryError as exc:
            check(str(exc) == "open-offer-obligation", "reviewed item tip-unlock remains an obligation")
        else:
            raise AssertionError("tip obligation accepted")

        # Real persisted timestamps drive explicit Hand back / Resume. Another fan's
        # command, a stale command, or a failed read must never cancel the human yield.
        from sqlalchemy import text
        import importlib
        db_engine = importlib.import_module("db.engine")
        resume_account, resume_fan = "900011", 900012
        async with get_session() as session:
            session.add(Account(id=resume_account))
            session.add(Fan(account_id=resume_account, fan_id=resume_fan))
            session.add(Message(account_id=resume_account, fan_id=resume_fan, message_id=99100000,
                                direction="out", body="synthetic human reply", created_at=now,
                                automation_kind=None))
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "CREATE TABLE IF NOT EXISTS of_control_audit (account_id TEXT, fan_id BIGINT, "
                "action TEXT, created_at DATETIME)")

        async def audit(fan_id, action, at):
            async with engine.begin() as connection:
                await connection.execute(text("INSERT INTO of_control_audit(account_id,fan_id,action,created_at) "
                                              "VALUES(:account,:fan,:action,:at)"),
                                         {"account": resume_account, "fan": fan_id, "action": action, "at": at})

        resume_engine = NativeEngine(resume_account, resume_fan, chat=chat, send=send)
        baseline = {"resume_after_manual_hours": 1}
        check(not await resume_engine._resumed_after_last_human(), "no explicit resume preserves human yield")
        await audit(resume_fan + 1, "release_takeover", now + timedelta(seconds=5))
        check(not await resume_engine._resumed_after_last_human(), "another fan's Hand back is isolated")
        await audit(resume_fan, "pause_automation", now + timedelta(seconds=5))
        check(not await resume_engine._resumed_after_last_human(), "Pause never satisfies human yield")
        await audit(resume_fan, "resume_automation", now - timedelta(seconds=1))
        check(not await resume_engine._resumed_after_last_human(), "stale resume preserves newer human message")
        await audit(resume_fan, "release_takeover", now)
        check(not await resume_engine._resumed_after_last_human(), "equal timestamps fail closed")
        await audit(resume_fan, "release_takeover", now + timedelta(seconds=1))
        check(await resume_engine._resumed_after_last_human(), "later Hand back satisfies human yield")
        config = await resume_engine.load_config(resume_account, baseline)
        check(config["resume_after_manual_hours"] == 0, "explicit Hand back reaches the native config")
        async with get_session() as session:
            session.add(Message(account_id=resume_account, fan_id=resume_fan, message_id=99100001,
                                direction="out", body="another synthetic human reply", automation_kind=None,
                                created_at=now + timedelta(seconds=2)))
        config = await resume_engine.load_config(resume_account, baseline)
        check(config["resume_after_manual_hours"] == 1, "newer human message reinstates human yield")
        def unavailable_session():
            raise RuntimeError("synthetic database unavailable")
        with patch.object(db_engine, "get_session", unavailable_session):
            config = await resume_engine.load_config(resume_account, baseline)
        check(config["resume_after_manual_hours"] == 1, "database failure leaves human yield intact")
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
