"""Twin's approved persona, creator facts and fan memory in the FASTT prompt.

Real ai_chatter prompt construction; only the model and the OnlyFans send are fake,
sockets are blocked. SQLite by default. Set NATIVE_PILOT_TEST_PG_URL to a freshly
bootstrapped (schema 0091) PostgreSQL database named s4c_* to run the same checks
there (tables are not created; rows use ids from NATIVE_PILOT_TEST_TAG and are left in place,
because history-memory corrections are append-only: use a fresh s4c_* database or a new tag per run).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
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
PG = os.environ.get("NATIVE_PILOT_TEST_PG_URL")
SHOW = os.environ.get("NATIVE_PILOT_TEST_SHOW")
TAG = int(os.environ.get("NATIVE_PILOT_TEST_TAG", "1"))

PERSONA = ("Mia is a warm, playful creator from the coast who loves surf mornings, cheap tacos "
           "and teasing her fans. She keeps things light, flirty and a little cheeky.")


def check(condition, label):
    global COUNT
    assert condition, label
    COUNT += 1


def system_of(call):
    return call["messages"][0]["content"]


async def exercise(tag):
    from sqlalchemy import select
    from db.engine import engine, get_session
    from db.models import (Base, Account, AccountAiConfig, Fan, Message, Employee,
                           OfCreatorBinding, CatalogItem)
    from db.onlyfans_models import OfRuntime
    from db.onlyfans_creator_facts_models import OfCreatorFactsSource
    from onlyfans_runtime.launch_history_memory.models import MemoryCorrection
    from onlyfans_runtime.native_context import canonical
    from onlyfans_runtime.creator_facts import SOURCE_COLUMNS
    from native_pilot.engine import NativeEngine, PilotBoundaryError, MODEL
    from native_pilot.twin_context import TwinPromptContext

    if not PG:
        # Twin's memory query uses PostgreSQL's md5(); give the SQLite fixture one.
        import hashlib
        from sqlalchemy import event
        event.listen(engine.sync_engine, "connect", lambda c, r: c.create_function(
            "md5", 1, lambda v: None if v is None else hashlib.md5(v.encode()).hexdigest()))
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    account = str(900000 + tag * 10)
    now = datetime.utcnow()
    fans = {n: int(account) * 10 + n for n in range(1, 8)}
    source = dict.fromkeys(SOURCE_COLUMNS)
    source.update(id="creator-" + account, updated_at=now.isoformat() + "Z",
                  date_of_birth="2000-01-02", hometown="Twinville", current_city="Twin Beach",
                  cover_story="Approved dance teacher", relationship_canon="single",
                  tattoos=False, tattoo_details=[], turn_ons=[], hard_limits=[])
    async with get_session() as s:
        s.add(Account(id=account, nickname="Synthetic creator"))
        if not PG:
            s.add(Employee(id=1, display_name="Automation", is_active=True))
        s.add(AccountAiConfig(account_id=account, persona=PERSONA,
                              ai_chatter_config_json=json.dumps({"pack_send_enabled":True,"pack_on_ask_enabled":True,"qualification_gate_enabled":True}),
                              persona_facts_json=json.dumps({"job": "Superseded job"})))
        s.add(OfCreatorBinding(id="bind-" + account, tenant_id="twin-test", creator_id="creator-" + account,
                               account_id=account))
        await s.flush()
        s.add(OfRuntime(account_id=account, binding_id="bind-" + account, enabled=False, activated_at=now))
        await s.flush()
        s.add(OfCreatorFactsSource(account_id=account, binding_id="bind-" + account,
                                   source_json=canonical(source), source_hash="a" * 64,
                                   source_updated_at=now, overrides_json=canonical({"home_city": "Override city"}),
                                   checked_at=now))
        asks = {1: "What are you up to tonight?", 2: "Wait, are you a bot or a real girl?",
                3: "hey you", 4: "hey you", 5: "hey you", 6: "Send the three dollar video please"}
        for n, text in asks.items():
            s.add(Fan(account_id=account, fan_id=fans[n]))
            s.add(Message(account_id=account, fan_id=fans[n], message_id=fans[n] * 10 + 1, direction="in",
                          body=text, created_at=now - timedelta(seconds=60)))
        await s.flush()
        s.add(MemoryCorrection(account_id=account, command_key="k-" + account, fan_id=fans[1], field="hobbies",
                               operation="set", value="rock climbing", before_json="{}", request_hash="b" * 64,
                               revision=1, actor="test", reason="synthetic", created_at=now))

    calls, sent = [], []
    async with get_session() as s:
        s.add(CatalogItem(id=tag * 10 + 2, account_id=account, label="Video", kind="video",
                          description_for_ai="One video, 21 seconds. No other content details are verified.",
                          media_ids="[1002]", duration_sec=21, price_cents=300, is_free_teaser=False, enabled=True))

    def make(fan, context, offer=False):
        async def chat(**kw):
            calls.append(kw)
            if kw.get("response_format"):
                return SimpleNamespace(content="{}", parsed={})
            if offer:
                return SimpleNamespace(content="This video is $3 to unlock.\n>>OFFER " + str(tag * 10 + 2), parsed=None)
            return SimpleNamespace(content="hey you, glad you stopped by", parsed=None)

        def send(fid, text, kw):
            sent.append((fid, text, kw))
            return {"id": fid * 100 + len(sent), "text": text}
        # A stub catalogue guard enables the existing native catalogue path (the
        # real ContentPlan is the runtime-mapping step's concern).
        content = SimpleNamespace(terms=lambda kw: dict(kw) if kw else None) if offer else None
        return NativeEngine(account, fan, chat=chat, send=send, context=context, content=content)

    async def prompt_for(n, context=TwinPromptContext, offer=False):
        calls.clear()
        with make(fans[n], context() if context else None, offer) as pilot:
            await pilot.run(n)
        main = [c for c in calls if not c.get("response_format")]
        check(len(main) == 1, f"one reply call for fan {n}")
        return main[0]

    # 1. ordinary turn: persona, effective facts and memory, each exactly once.
    call = await prompt_for(1)
    system, whole = system_of(call), json.dumps(call["messages"])
    if SHOW:
        print(system, "\n-----\n", call["messages"][-2]["content"][:600], "\n-----\n", call["messages"][-1]["content"], file=sys.stderr)
    check(system.count("surf mornings") == 1, "approved persona exactly once")
    check(system.count("Approved dance teacher") == 1, "Twin source job exactly once")
    check(system.count("Superseded job") == 0, "saved job superseded by the Twin source")
    check(system.count("Override city") == 1 and "Twinville" not in system, "explicit override wins, once")
    check(system.count("THESE ARE THE FACTS ABOUT YOU") == 1, "one facts table, shown on a normal turn")
    check(whole.count("rock climbing") == 1 and whole.count("Historical memory evidence") == 1,
          "operator memory appears once, as one evidence message")
    check(len(call["messages"]) == 4 and call["messages"][-1]["role"] == "user"
          and call["messages"][-2]["content"].startswith("Historical memory evidence"),
          "memory sits just before the current turn")
    check("100% human" not in system and "never a bot" not in system.lower()
          and "NEVER plead memory loss or explain yourself as a bot" not in system
          and "TEXT LIKE A REAL PERSON" not in system and "a real 2" not in system,
          "no FASTT human-claim wording")
    check(system.count("honestly that you are the creator's AI chat clone") == 1
          and system.count("Explain your AI identity honestly when asked") == 1,
          "truthful identity wording")
    check(system.count("Never state what a photo or video shows") == 1, "undescribed media rule present once")
    check(sent and sent[-1][0] == fans[1], "the FASTT path still sends")

    # 2. bot question: the turn instruction answers honestly instead of brushing off.
    call = await prompt_for(2)
    system = system_of(call)
    check("answer honestly and directly that you are an AI chat clone" in system, "honest answer instruction")
    check("defending is what a bot does" not in system and "breezy brush-off" not in system,
          "FASTT brush-off instruction absent")
    check("Historical memory evidence" not in json.dumps(call["messages"]), "no memory block when nothing to add")

    # 3. without the context the native FASTT prompt is unchanged (opt-in proof).
    call = await prompt_for(3, context=None)
    check("100% human" in system_of(call) and "Approved dance teacher" not in system_of(call),
          "native prompt untouched without the hook")

    # 4. stale shared facts: refuse before any model call or send.
    async with get_session() as s:
        row = await s.get(OfCreatorFactsSource, account)
        row.checked_at = now - timedelta(hours=1)
    before = (len(calls), len(sent))
    try:
        await prompt_for(4)
    except PilotBoundaryError as e:
        check(str(e) == "creator-facts-not-current", "stale facts reason")
    else:
        raise AssertionError("stale creator facts accepted")
    check((len(calls), len(sent)) == (0, before[1]), "no model call or send on stale facts")

    # 5. a persona that claims to be human is not approved.
    async with get_session() as s:
        row = await s.get(OfCreatorFactsSource, account)
        row.checked_at = now
        (await s.get(AccountAiConfig, account)).persona = PERSONA + " You are 100% human, never a bot."
    sent_before = len(sent)
    try:
        await prompt_for(5)
    except PilotBoundaryError as e:
        check(str(e) == "creator-persona-not-approved", "unapproved persona reason")
    else:
        raise AssertionError("unapproved persona accepted")
    check(len(sent) == sent_before, "no send for an unapproved persona")

    # 6. the existing priced-card path still works, with the same prompt hook.
    async with get_session() as s:
        row = await s.get(AccountAiConfig, account)
        row.persona = PERSONA
    call = await prompt_for(6, offer=True)
    system = system_of(call)
    check(system.count("surf mornings") == 1 and "100% human" not in system, "persona once, no human claim, with a live card")
    check("One video, 21 seconds" in system and system.count("Never state what a photo or video shows") == 1,
          "the described catalogue item and the media rule are both in the prompt")
    card = [kw for _, _, kw in sent if kw]
    check(len(card) == 1 and card[0].get("media_files") == [1002] and card[0].get("price") == 3,
          "the $3 card is still sent by FASTT's selling path")

    await engine.dispose()


def main():
    import dotenv
    dotenv.load_dotenv = lambda *a, **kw: False
    logging.disable(logging.CRITICAL)
    with tempfile.TemporaryDirectory(prefix="native-context-test-") as tmp:
        url = PG or "sqlite+aiosqlite:///" + (Path(tmp) / "fixture.db").as_posix()
        with patch.dict(os.environ, {"DATABASE_URL": url, "PYTHON_DOTENV_DISABLED": "1"}, clear=True):
            async def guarded():
                real = socket.socket.connect

                def deny(self, addr, *a, **kw):
                    if PG and isinstance(addr, tuple) and addr[0] in ("127.0.0.1", "::1", "localhost"):
                        return real(self, addr, *a, **kw)
                    raise AssertionError("network forbidden in context fixture")
                with patch.object(socket.socket, "connect", deny), patch.object(socket.socket, "connect_ex", deny):
                    await exercise(TAG)
            asyncio.run(guarded())
    print(f"native pilot context: {COUNT} checks; all cases passed")


if __name__ == "__main__":
    main()
