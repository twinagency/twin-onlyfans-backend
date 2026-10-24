"""FASTT's real ai_chatter must stop at the wire, bubble by bubble, after pacing.

Real native engine; only the model and the provider send are fake. A scripted
hold (the pacing/typing wait) lets a pause, a human answer or a new inbound land
exactly where the old once-only `_thread_moved_on` could not see it.

SQLite by default (temp file). Set FASTT_WIRE_TEST_DB to an EMPTY disposable
`postgresql+asyncpg://...` database to run the identical checks on PostgreSQL.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

COUNT = 0
REPLY = "Glad you stopped by.\n\nMy morning was lazy, coffee and nothing else.\n\nHow was yours?"
ACCOUNT = "900001"


def check(condition, label):
    global COUNT
    assert condition, label
    COUNT += 1


async def exercise():
    from sqlalchemy import select, delete
    from db.engine import engine, get_session
    from db.models import Base, Account, Fan, Message, Employee, SkipList
    from automations import ai_chatter as native
    from automations._common import (set_automation_restrict, set_of_restricted_skip,
                                     mark_muted_creator_skip, _skip_and_rest)
    from native_pilot.engine import NativeEngine

    tables = [t for t in Base.metadata.sorted_tables if not t.name.startswith("twin_bridge_")]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    async with get_session() as s:
        s.add(Account(id=ACCOUNT, nickname="Synthetic creator"))
        s.add_all([Employee(id=1, display_name="Automation", is_active=True),
                   Employee(id=2, display_name="Chatter", is_active=True)])

    fans = iter(range(900100, 900200))
    msg_ids = iter(range(5000, 6000))

    async def new_fan(*, inbound_age=60):
        fan = next(fans)
        async with get_session() as s:
            s.add(Fan(account_id=ACCOUNT, fan_id=fan))
            s.add(Message(account_id=ACCOUNT, fan_id=fan, message_id=next(msg_ids), direction="in",
                          body="How was your morning?",
                          created_at=datetime.utcnow() - timedelta(seconds=inbound_age)))
        return fan

    async def play(fan, *, on_hold=None, on_chat=None):
        """One real ai_chatter turn. on_hold(n) runs INSIDE the n-th pacing wait."""
        sent, holds = [], []

        async def chat(**kw):
            if kw.get("response_format"):
                return SimpleNamespace(content="{}", parsed={})
            if on_chat:
                await on_chat()
            return SimpleNamespace(content=REPLY, parsed=None)

        def send(fid, text, kw):
            sent.append(text)
            return {"id": 80000000 + next(msg_ids), "text": text}

        class Paced(NativeEngine):
            async def hold(self, account_id, fan_id, seconds, **kw):
                holds.append(seconds)
                if on_hold:
                    await on_hold(len(holds))

        with Paced(ACCOUNT, fan, chat=chat, send=send) as pilot:
            stats = await pilot.run(1)
        return sent, stats

    async def add_skip(fan, reason):
        async with get_session() as s:
            await s.merge(SkipList(account_id=ACCOUNT, fan_id=fan, reason=reason,
                                   added_at=datetime.utcnow()))

    async def skip_reason(fan):
        async with get_session() as s:
            return (await s.execute(select(SkipList.reason).where(
                SkipList.account_id == ACCOUNT, SkipList.fan_id == fan))).scalar_one_or_none()

    # -- baseline: nothing interferes, all three bubbles go out ---------------
    fan = await new_fan()
    sent, stats = await play(fan)
    check(len(sent) == 3 and not stats["stale_drops"], "baseline sends every bubble, no drop")

    # -- pause lands during GENERATION ---------------------------------------
    fan = await new_fan()
    async def pause_now(fan=fan):
        await add_skip(fan, "manual_restrict")
    sent, stats = await play(fan, on_chat=pause_now)
    check(sent == [] and stats["replies_sent"] == 0, "pause during generation: nothing sent")

    # -- pause lands during the FIRST pacing wait ------------------------------
    fan = await new_fan()
    async def pause_in_first_hold(n, fan=fan):
        if n == 1:
            await add_skip(fan, "manual_restrict")
    sent, stats = await play(fan, on_hold=pause_in_first_hold)
    check(sent == [], "pause during first pacing wait: nothing sent")
    check(stats["stale_drops"] == {"operator_stop": 1}, "counted as an operator stop")

    # -- pause lands between bubbles: LATER bubbles are stopped ---------------
    fan = await new_fan()
    async def pause_in_second_hold(n, fan=fan):
        if n == 2:
            await add_skip(fan, "manual_restrict")
    sent, stats = await play(fan, on_hold=pause_in_second_hold)
    check(sent == ["Glad you stopped by"], "pause after bubble 1: bubbles 2 and 3 never sent")
    check(stats["stale_drops"] == {"operator_stop": 1}, "later-bubble stop counted")

    # -- pause lands before the LAST bubble (the one that would carry an offer) -
    fan = await new_fan()
    async def pause_in_third_hold(n, fan=fan):
        if n == 3:
            await add_skip(fan, "manual_restrict")
    sent, _ = await play(fan, on_hold=pause_in_third_hold)
    check(len(sent) == 2, "pause before the last bubble: last bubble never sent")

    # -- a human answers during pacing (named chatter) -------------------------
    fan = await new_fan()
    async def chatter_answers(n, fan=fan):
        if n == 1:
            async with get_session() as s:
                s.add(Message(account_id=ACCOUNT, fan_id=fan, message_id=next(msg_ids), direction="out",
                              body="hey it's me", sent_by_employee_id=2,
                              created_at=datetime.utcnow()))
    sent, stats = await play(fan, on_hold=chatter_answers)
    check(sent == [] and sum(stats["stale_drops"].values()) == 1,
          "named chatter answered during pacing: AI does not talk over them")

    # -- an UNATTRIBUTED outbound (official app / manual path) during pacing ---
    fan = await new_fan()
    async def unattributed(n, fan=fan):
        if n == 2:
            async with get_session() as s:
                s.add(Message(account_id=ACCOUNT, fan_id=fan, message_id=next(msg_ids), direction="out",
                              body="typed in the app", created_at=datetime.utcnow()))
    sent, stats = await play(fan, on_hold=unattributed)
    check(sent == ["Glad you stopped by"] and stats["stale_drops"] == {"manual_activity": 1},
          "unattributed human outbound between bubbles stops the rest")

    # -- the fan writes again during pacing -------------------------------------
    fan = await new_fan()
    async def fan_again(n, fan=fan):
        if n == 1:
            async with get_session() as s:
                s.add(Message(account_id=ACCOUNT, fan_id=fan, message_id=next(msg_ids), direction="in",
                              body="wait one more thing", created_at=datetime.utcnow()))
    sent, stats = await play(fan, on_hold=fan_again)
    check(sent == [] and stats["stale_drops"] == {"fan_spoke_again": 1}, "changed input stops the send")

    # -- stronger muted/of_restricted rows also stop mid-reply ------------------
    fan = await new_fan()
    async def restricted(n, fan=fan):
        if n == 2:
            await add_skip(fan, "of_restricted")
    sent, _ = await play(fan, on_hold=restricted)
    check(len(sent) == 1, "of_restricted mid-reply stops the rest")

    # -- an explicit stop outlives the timed 1h human cooldown -------------------
    fan = await new_fan()
    old = datetime.utcnow() - timedelta(hours=5)
    async with get_session() as s:
        # a human spoke 5h ago: far outside resume_after_manual_hours (1h)
        s.add(Message(account_id=ACCOUNT, fan_id=fan, message_id=next(msg_ids), direction="out",
                      body="earlier human line", sent_by_employee_id=2, created_at=old - timedelta(seconds=5)))
        s.add(SkipList(account_id=ACCOUNT, fan_id=fan, reason="manual_restrict", added_at=old))
    sent, stats = await play(fan)
    check(sent == [] and stats["replies_sent"] == 0, "manual stop 5h old still silences the fan")
    check(await skip_reason(fan) == "manual_restrict", "nothing aged the stop out")
    async with get_session() as s:
        await s.execute(delete(SkipList).where(SkipList.account_id == ACCOUNT, SkipList.fan_id == fan))
    sent, _ = await play(fan)
    check(len(sent) == 3, "once explicitly released the fan is answered (cooldown long over)")

    # -- the same cooldown, when it is the ONLY thing: still enforced -----------
    fan = await new_fan()
    async with get_session() as s:
        s.add(Message(account_id=ACCOUNT, fan_id=fan, message_id=next(msg_ids), direction="out",
                      body="recent human", sent_by_employee_id=2,
                      created_at=datetime.utcnow() - timedelta(minutes=30)))
        s.add(Message(account_id=ACCOUNT, fan_id=fan, message_id=next(msg_ids), direction="in",
                      body="hello again", created_at=datetime.utcnow() - timedelta(seconds=20)))
    sent, stats = await play(fan)
    check(sent == [], "timed human cooldown still works on its own")

    # -- skip_list writers: both dialects, and nothing erases an operator stop ---
    fan = await new_fan()
    await set_automation_restrict(ACCOUNT, fan, True)
    check(await skip_reason(fan) == "manual_restrict", "set_automation_restrict inserts")
    await set_automation_restrict(ACCOUNT, fan, True)
    check(await skip_reason(fan) == "manual_restrict", "set_automation_restrict upserts")
    await mark_muted_creator_skip(ACCOUNT, fan)
    check(await skip_reason(fan) == "manual_restrict", "auto mute never overwrites a manual stop")
    await _skip_and_rest(ACCOUNT, fan, datetime.utcnow())
    check(await skip_reason(fan) == "manual_restrict", "unreachable never overwrites a manual stop")
    await set_of_restricted_skip(ACCOUNT, fan, True)
    check(await skip_reason(fan) == "of_restricted", "of_restricted is stronger and wins")
    await set_automation_restrict(ACCOUNT, fan, True)
    check(await skip_reason(fan) == "of_restricted", "manual stop never demotes of_restricted")
    await set_automation_restrict(ACCOUNT, fan, False)
    check(await skip_reason(fan) == "of_restricted", "release removes ONLY manual_restrict")
    await set_of_restricted_skip(ACCOUNT, fan, False)
    check(await skip_reason(fan) is None, "of_restricted cleared on its own")
    await _skip_and_rest(ACCOUNT, fan, datetime.utcnow())
    check(await skip_reason(fan) == "unreachable", "unreachable upserts on this dialect")
    await set_automation_restrict(ACCOUNT, fan, True)
    await set_automation_restrict(ACCOUNT, fan, False)
    check(await skip_reason(fan) is None, "manual stop replaces then releases cleanly")

    # -- pause acknowledged while the model call is HUNG: it lands first, the late reply is never sent ------
    fan = await new_fan()
    release, started = asyncio.Event(), asyncio.Event()
    async def hung():
        started.set()
        await release.wait()
    hung_turn = asyncio.ensure_future(play(fan, on_chat=hung))
    await asyncio.wait_for(started.wait(), 10)
    await add_skip(fan, "manual_restrict")                 # the acknowledged Pause
    release.set()
    sent, stats = await asyncio.wait_for(hung_turn, 10)
    check(sent == [] and stats["replies_sent"] == 0, "pause acked during a hung model call: nothing sent after it")

    await engine.dispose()


def main():
    import dotenv
    dotenv.load_dotenv = lambda *a, **kw: False
    logging.disable(logging.CRITICAL)
    external = os.environ.get("FASTT_WIRE_TEST_DB")
    with tempfile.TemporaryDirectory(prefix="fastt-wire-stop-") as tmp:
        url = external or "sqlite+aiosqlite:///" + (Path(tmp) / "fixture.db").as_posix()
        with patch.dict(os.environ, {"DATABASE_URL": url, "PYTHON_DOTENV_DISABLED": "1"}, clear=True):
            asyncio.run(exercise())
    print(f"{COUNT} checks; all cases passed ({'postgresql' if external else 'sqlite'})")


if __name__ == "__main__":
    main()
