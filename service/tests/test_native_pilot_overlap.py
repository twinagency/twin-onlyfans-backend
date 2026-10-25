"""A fan message arriving during a provider POST remains unanswered exactly once.

Synthetic SQLite history, real ledger/selector/native engine; no network or credentials.
"""
import asyncio
import json
import logging
import os
from pathlib import Path
import socket
import sys
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_native_pilot_ledger as L
from native_pilot.overlap import older_reply_sql


def ledger_cases(root):
    from native_pilot.ledger import Ledger
    for case in ("recovery", "human", "unknown", "unclaimed", "already-seen", "scope", "same-second"):
        f = L.Fixture(root, case)
        f.activate()
        f.source(100)
        key = f.claim()
        attempt = f.attempt(key)
        # The HTTP POST is in progress. Another fan message arrives after the
        # input snapshot was claimed, but before the outgoing provider timestamp.
        created = L.NOW + timedelta(seconds=11)
        f.source(101, body="What are you doing today?", created_at=created.isoformat(sep=" "),
                 ingested_at=(L.NOW + timedelta(seconds=12)).isoformat(sep=" "))
        f.call["now"] = L.NOW + timedelta(seconds=14)
        f.sent(attempt, 102)
        f.source(102, direction="out", body="Ben it is", created_at=(created if case == "same-second" else
                 L.NOW + timedelta(seconds=13)).isoformat(sep=" "))
        f.ledger.finish_input(**f.call, input_key=key)
        if case == "human":
            f.source(103, direction="out", body="A human answered", created_at=(L.NOW + timedelta(seconds=14)).isoformat(sep=" "))
        elif case == "unknown":
            f.sql("UPDATE native_pilot_receipts SET state='delivery_unknown'")
        elif case == "unclaimed":
            f.sql("UPDATE native_pilot_inputs SET state='generating'")
        elif case == "already-seen":
            f.sql("UPDATE messages SET ingested_at=? WHERE message_id=101", ((L.NOW + timedelta(seconds=9)).isoformat(sep=" "),))
        elif case == "scope":
            f.sql("UPDATE native_pilots SET fan_id=fan_id+1")
        query = ("SELECT m.message_id FROM messages m WHERE m.direction='in' AND NOT EXISTS "
                 "(SELECT 1 FROM messages n WHERE n.account_id=m.account_id AND n.fan_id=m.fan_id "
                 "AND (n.created_at>m.created_at OR (n.created_at=m.created_at AND n.message_id>m.message_id)) "
                 "AND NOT (" + older_reply_sql() + "))")
        pending = f.sql(query)
        if case not in ("recovery", "same-second"):
            assert pending == [], (case, pending)
            if case != "scope":
                assert f.ledger.snapshot(**f.call) is None, case
            continue
        assert pending == [(101,)], (case, pending)
        from native_pilot.service import Service
        from native_pilot.scope import AccountScope
        f.sql("CREATE TABLE of_conversation_state(account_id TEXT,fan_id INTEGER,mode TEXT,takeover_state TEXT)")
        f.sql("INSERT INTO of_conversation_state VALUES(?,?,'automated','none')", (L.ACCT, L.FAN))
        svc = Service.__new__(Service)
        svc.db, svc.path, svc.started_at = L.ACCT, f.path, L.NOW
        svc.scope, svc.fan_holds, svc._owned = AccountScope.parse(L.ACCT), {}, lambda fan: True
        assert svc.pending_fans()[0] == [(L.FAN, 101)], "real all-fans selector recovers input"
        for mode, takeover in (("paused", "none"), ("automated", "active")):
            f.sql("UPDATE of_conversation_state SET mode=?,takeover_state=?", (mode, takeover))
            assert svc.pending_fans()[0] == [], "operator hold remains authoritative"
        f.sql("UPDATE of_conversation_state SET mode='automated',takeover_state='none'")
        # A new process sees the same durable evidence; no timestamps are changed.
        f.ledger = Ledger(f.path)
        snap = f.ledger.snapshot(**f.call)
        assert snap.message_id == 101 and snap.member_ids == (101,), snap
        second = f.ledger.claim_input(**f.call, snapshot=snap)
        assert f.ledger.unanswered_reply_ids(**f.call, input_key=second) == (102,)
        again = f.attempt(second)
        f.sent(again, 104)
        f.source(104, direction="out", body="Answer to the question", created_at=(L.NOW + timedelta(seconds=14)).isoformat(sep=" "))
        f.ledger.finish_input(**f.call, input_key=second)
        assert f.ledger.snapshot(**f.call) is None, "confirmed answer cannot be regenerated"
        assert f.sql(query) == [], "answered input must leave candidate selection"
    print("overlap ledger: recovery, restart, exactly-once and barrier cases passed")


async def engine_case():
    from sqlalchemy import select, delete
    from db.engine import engine, get_session
    from db.models import Base, Account, Fan, Message, Employee
    from native_pilot.engine import NativeEngine
    from automations import ai_chatter as native
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    account, fan = "900010", 900011
    now = datetime.utcnow()
    incoming = now - timedelta(seconds=15)
    async with get_session() as s:
        s.add(Account(id=account, nickname="Synthetic creator"))
        s.add(Fan(account_id=account, fan_id=fan))
        s.add(Employee(id=1, display_name="Automation", is_active=True))
        s.add_all([
            Message(account_id=account, fan_id=fan, message_id=100, direction="in", body="Call me Ben", created_at=now-timedelta(seconds=30)),
            Message(account_id=account, fan_id=fan, message_id=101, direction="in", body="What are you doing today?", created_at=incoming),
            Message(account_id=account, fan_id=fan, message_id=102, direction="out", body="Ben it is", automation_kind="ai_chatter", created_at=incoming+timedelta(seconds=1)),
        ])
    calls, sends = [], []
    async def chat(**kwargs):
        calls.append(kwargs)
        if kwargs.get("response_format"):
            return SimpleNamespace(content="{}", parsed={})
        return SimpleNamespace(content="Chatting here with you. What are your plans?", parsed=None)
    def send(fid, body, kw):
        sends.append((fid, body))
        return {"id": 200 + len(sends), "text": body}
    with NativeEngine(account, fan, chat=chat, send=send) as pilot:
        gathered = await native._gather(account, {fan})
        assert gathered[fan].last_dir == "out", "default behavior unchanged"
        assert await native._thread_moved_on(account, fan, incoming) == "already_answered"
    with NativeEngine(account, fan, chat=chat, send=send, unanswered_reply_ids=(102,)) as pilot:
        gathered = await native._gather(account, {fan})
        assert gathered[fan].last_dir == "in" and gathered[fan].last_body == "What are you doing today?"
        assert "Ben it is" in str(gathered[fan].messages), "real outgoing remains in model history"
        assert not gathered[fan].answered_after_in
        assert gathered[fan].pic_back_at is None, "older text is not a picture reaction"
        assert await native._thread_moved_on(account, fan, incoming) == ""
        stats = await pilot.run(1)
        assert stats["replies_sent"] == 1 and sends, stats
        assert "What are you doing today?" in str([x["messages"] for x in calls])
        assert await native._thread_moved_on(account, fan, incoming) == "already_answered", "new real reply still blocks"
    async with get_session() as s:
        # A named human is never hidden, even if a stale receipt exclusion names
        # that same message id. The gather and wire gates must agree.
        s.add(Employee(id=2, display_name="Human tester", is_active=True))
        await s.execute(delete(Message).where(Message.account_id == account, Message.message_id >= 200))
        row = await s.get(Message, (account, fan, 102))
        row.automation_kind = None
        row.sent_by_employee_id = 2
    with NativeEngine(account, fan, chat=chat, send=send, unanswered_reply_ids=(102,)):
        assert (await native._gather(account, {fan}))[fan].last_dir == "out"
        assert await native._thread_moved_on(account, fan, incoming) == "already_answered"
    await engine.dispose()
    print("overlap native engine: actual question answered; history and human guards preserved")


def main():
    import dotenv
    dotenv.load_dotenv = lambda *a, **kw: False
    logging.disable(logging.CRITICAL)
    with tempfile.TemporaryDirectory(prefix="native-overlap-") as tmp:
        root = Path(tmp)
        ledger_cases(root)
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite+aiosqlite:///" + str(root / "engine.db"),
                                     "PYTHON_DOTENV_DISABLED": "1"}, clear=True):
            async def guarded():
                def deny(*args, **kwargs):
                    raise AssertionError("network forbidden")
                with patch.object(socket.socket, "connect", deny), patch.object(socket, "create_connection", deny):
                    await engine_case()
            asyncio.run(guarded())


if __name__ == "__main__":
    main()
