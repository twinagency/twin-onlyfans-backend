"""FASTT-path sends reach Twin's dashboard tables once; unknown stays unknown.

The real ai_chatter writes the ``messages`` row; ``native_pilot.reporting`` adds the
delivery receipt and the unresolved/attention markers; the dashboard's own read
repository and projections read the result back. Model and OnlyFans send are fake,
sockets are blocked. SQLite by default; NATIVE_PILOT_TEST_PG_URL (a bootstrapped
0091 database named s4c_*) runs the same checks on PostgreSQL.
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
PG = os.environ.get("NATIVE_PILOT_TEST_PG_URL")
TAG = int(os.environ.get("NATIVE_PILOT_TEST_TAG", "2"))


def check(condition, label):
    global COUNT
    assert condition, label
    COUNT += 1


async def exercise(tag):
    from sqlalchemy import func, select, text
    from db.engine import engine, get_session
    from db.models import (Base, Account, Fan, Message, Employee, OfMessageDelivery, OfConversationState)
    from native_pilot.engine import NativeEngine
    from native_pilot.reporting import report_outcomes, attempt_ref
    from onlyfans_read import projections
    from onlyfans_read.repo import SqlOnlyFansReadRepo

    if not PG:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    account = str(910000 + tag * 10)
    fan_ok, fan_unknown, fan_error = (int(account) * 10 + n for n in (1, 2, 3))
    now = datetime.utcnow()
    async with get_session() as s:
        s.add(Account(id=account, nickname="Synthetic creator"))
        if not PG:
            s.add(Employee(id=1, display_name="Automation", is_active=True))
        for fan in (fan_ok, fan_unknown, fan_error):
            s.add(Fan(account_id=account, fan_id=fan))
        s.add(Message(account_id=account, fan_id=fan_ok, message_id=fan_ok * 10 + 1, direction="in",
                      body="How was your morning?", created_at=now - timedelta(seconds=60)))

    sent = []

    async def chat(**kw):
        if kw.get("response_format"):
            return SimpleNamespace(content="{}", parsed={})
        return SimpleNamespace(content="Glad you stopped by", parsed=None)

    def send(fid, text_, kw):
        sent.append((fid, text_))
        return {"id": fid * 100 + len(sent), "text": text_, "createdAt": datetime.utcnow().isoformat()}

    async def counts(fan):
        async with get_session() as s:
            m = await s.scalar(select(func.count()).select_from(Message).where(
                Message.account_id == account, Message.fan_id == fan, Message.direction == "out"))
            d = await s.scalar(select(func.count()).select_from(OfMessageDelivery).where(
                OfMessageDelivery.account_id == account, OfMessageDelivery.fan_id == fan))
            st = await s.scalar(select(func.count()).select_from(OfConversationState).where(
                OfConversationState.account_id == account, OfConversationState.fan_id == fan))
        return m, d, st

    # --- accepted send through the real FASTT engine ------------------------------
    with NativeEngine(account, fan_ok, chat=chat, send=send) as pilot:
        stats = await pilot.run(1)
    check(len(sent) == 1 and not stats.get("errors"), ("FASTT sent one reply", sent, stats))
    provider_id = sent[0][0] * 100 + 1
    check(await counts(fan_ok) == (1, 0, 0), "FASTT wrote exactly its own messages row; no receipt yet")
    receipts = [{"attempt_id": 1, "state": "sent", "provider_message_id": str(provider_id)}]
    first = await report_outcomes(account_id=account, fan_id=fan_ok, receipts=receipts)
    check(first["recorded"] == 1 and first["conflicts"] == 0 and first["missing_message"] == 0, "receipt recorded")
    check(await counts(fan_ok) == (1, 1, 0), "one message, one receipt, control state untouched")
    second = await report_outcomes(account_id=account, fan_id=fan_ok, receipts=receipts)
    check(second["recorded"] == 0 and second["unchanged"] == 1, "repeat report is a no-op")
    check(await counts(fan_ok) == (1, 1, 0), "no duplicate rows after a repeat")

    repo = SqlOnlyFansReadRepo()
    messages = await repo.messages(account_id=account, fan_id=fan_ok, limit=20)
    deliveries = await repo.deliveries(account_id=account, fan_id=fan_ok)
    outbound = [m for m in messages if m.direction == "out"]
    model = projections.message_model(conversation_id="c", message=outbound[0], recorded=deliveries.get(outbound[0].message_id))
    check(model["origin"] == "ai" and model["delivery"] == "sent" and model["evidenceRef"] == attempt_ref(1),
          "dashboard shows the FASTT send as an AI-sent, accepted bubble with its evidence")
    check(deliveries[provider_id].origin == "ai" and deliveries[provider_id].attempt_id == attempt_ref(1),
          "receipt origin and attempt")

    # --- a conflicting receipt is never overwritten --------------------------------
    clash = await report_outcomes(account_id=account, fan_id=fan_ok,
                                  receipts=[{"attempt_id": 7, "state": "sent", "provider_message_id": str(provider_id)}])
    check(clash["conflicts"] == 1 and clash["recorded"] == 0, "a different attempt cannot rewrite a receipt")
    check((await repo.deliveries(account_id=account, fan_id=fan_ok))[provider_id].attempt_id == attempt_ref(1),
          "original receipt kept")

    # --- an accepted id with no FASTT messages row is flagged, not invented ---------
    missing = await report_outcomes(account_id=account, fan_id=fan_error,
                                    receipts=[{"attempt_id": 5, "state": "sent", "provider_message_id": "424242"}])
    check(missing["missing_message"] == 1 and await counts(fan_error) == (0, 0, 1), "no invented message; attention only")

    # --- unknown outcome: no message, no 'sent', held and visible as unknown -------
    unknown = await report_outcomes(account_id=account, fan_id=fan_unknown, receipts=[
        {"attempt_id": 9, "state": "delivery_unknown", "provider_message_id": None}])
    check(unknown["unknown"] == 1 and unknown["recorded"] == 0, "unknown counted, never recorded as sent")
    check(await counts(fan_unknown) == (0, 0, 1), "no message or receipt for an unproven send")
    state = await repo.conversation_state(account_id=account, fan_id=fan_unknown)
    check(state.unresolved_attempt_id == attempt_ref(9) and state.requires_attention, "unresolved attempt held")
    incident = projections.delivery_incident(state=state, messages=[], deliveries={})
    check(incident and incident["outcome"] == "unknown" and incident["attemptId"] == attempt_ref(9),
          "dashboard incident says unknown")
    again = await report_outcomes(account_id=account, fan_id=fan_unknown, receipts=[
        {"attempt_id": 9, "state": "dispatching", "provider_message_id": None}])
    check(again["unknown"] == 1 and (await counts(fan_unknown)) == (0, 0, 1), "a stale dispatching row is unknown too")
    async with get_session() as s:
        row = await s.get(OfConversationState, (account, fan_unknown))
        row.unresolved_attempt_id = "fastt-pilot-1"
    await report_outcomes(account_id=account, fan_id=fan_unknown, receipts=[
        {"attempt_id": 10, "state": "delivery_unknown", "provider_message_id": None}])
    check((await repo.conversation_state(account_id=account, fan_id=fan_unknown)).unresolved_attempt_id == "fastt-pilot-1",
          "an existing hold is never replaced")

    # --- an error flags attention without inventing a hold --------------------------
    await report_outcomes(account_id=account, fan_id=fan_ok, receipts=receipts, run_error="native-turn-failed")
    state = await repo.conversation_state(account_id=account, fan_id=fan_ok)
    check(state.requires_attention and state.unresolved_attempt_id is None, "error raises attention only")
    check(await counts(fan_ok) == (1, 1, 1), "still one message and one receipt")

    if PG:
        async with engine.begin() as conn:
            for stmt in ("DELETE FROM of_message_delivery WHERE account_id=:a",
                         "DELETE FROM of_conversation_state WHERE account_id=:a"):
                await conn.execute(text(stmt), {"a": account})
    await engine.dispose()


def main():
    import dotenv
    dotenv.load_dotenv = lambda *a, **kw: False
    logging.disable(logging.CRITICAL)
    with tempfile.TemporaryDirectory(prefix="native-report-test-") as tmp:
        url = PG or "sqlite+aiosqlite:///" + (Path(tmp) / "fixture.db").as_posix()
        with patch.dict(os.environ, {"DATABASE_URL": url, "PYTHON_DOTENV_DISABLED": "1"}, clear=True):
            async def guarded():
                real = socket.socket.connect

                def deny(self, addr, *a, **kw):
                    if PG and isinstance(addr, tuple) and addr[0] in ("127.0.0.1", "::1", "localhost"):
                        return real(self, addr, *a, **kw)
                    raise AssertionError("network forbidden in reporting fixture")
                with patch.object(socket.socket, "connect", deny), patch.object(socket.socket, "connect_ex", deny):
                    await exercise(TAG)
            asyncio.run(guarded())
    print(f"native pilot reporting: {COUNT} checks; all cases passed")


if __name__ == "__main__":
    main()
