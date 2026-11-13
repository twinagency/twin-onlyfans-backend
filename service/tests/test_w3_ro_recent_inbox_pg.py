"""W3 runtime-offers red tests: recent_inbox failed-fan handling (REVIEW inv.14, inv.7).

Complements rwl/W3-runtime-core-b test_w3_rcb_recent_inbox_retry_pg.py (attempt cap in one
process). These two add: (1) a fan-specific Refused must not pin every other fan behind it;
(2) the give-up must survive a worker restart (the retry state is process memory today).
Synthetic PG only; reader I/O is monkeypatched, no network, no sends.
"""
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database  # noqa: F401 (fixture)
from test_onlyfans_launch_provider_budget_pg import native  # noqa: F401 (fixture)
from test_onlyfans_connections_pg import TENANT
from db.models import Message
from onlyfans_runtime.store import Event, Refused
from onlyfans_runtime.worker import ChatPage, MessagePage
from onlyfans_runtime.recent_inbox import poll_recent, INTERVAL_SECONDS
from onlyfans_runtime.native_protocol import NativeProtocolError
import onlyfans_runtime.provider_read_budget as budget

BAD, GOOD = 911, 912
MAX_ATTEMPTS = 5


def _fake_clock(monkeypatch, start=100.0):
    now = [start]
    wall0 = budget.utcnow()
    # The shared read budget refills on wall time; drive it from the fake clock.
    monkeypatch.setattr(budget, "utcnow", lambda: wall0 + timedelta(seconds=now[0]))
    return now


def _event(fan, mid):
    return Event(fan, mid, datetime.now(timezone.utc).isoformat(), "in", f"Synthetic message {mid}")


def test_fan_specific_refusal_does_not_pin_the_inbox(native, monkeypatch):
    """Was red: recent_inbox re-raised Refused before popleft, so BAD stayed pending[0]."""
    store, account, reader = native
    now = _fake_clock(monkeypatch)
    monkeypatch.setattr(reader, "chats", lambda **kw: ChatPage([BAD, GOOD], None, {BAD: 300, GOOD: 400}))
    calls = {BAD: 0, GOOD: 0}

    def messages(**kw):
        calls[kw["fan_id"]] += 1
        if kw["fan_id"] == BAD:
            # A page whose rows belong to another fan: _commit refuses it ("invalid inbox
            # message page") -- a property of this one conversation, not of the account.
            return MessagePage([_event(GOOD, 300)], False)
        return MessagePage([_event(GOOD, 400)], False)

    monkeypatch.setattr(reader, "messages", messages)
    # inbox_worker.py:111-118 catches any error and re-polls the account 1 s later.
    for _ in range(120):
        try:
            poll_recent(store, TENANT, account, reader, connection_revision=1, clock=lambda: now[0])
        except Refused:
            pass
        now[0] += 1
    with Session(store.engine) as s:
        imported = s.get(Message, (account, GOOD, 400)) is not None
    assert imported, (f"inv.7/14: one refused conversation pinned the inbox for 120 s: "
                      f"BAD read {calls[BAD]}x, GOOD read {calls[GOOD]}x, GOOD's new message never imported")
    assert calls[BAD] <= MAX_ATTEMPTS, f"refused fan re-read {calls[BAD]}x in 120 s"


@pytest.mark.xfail(strict=True, reason="G03 follow-up: per-fan give-up is process memory "
                   "(HeadState.attempts); restart durability needs a schema column")
def test_failed_fan_give_up_survives_worker_restart(native, monkeypatch):
    """HeadState.retry_at/failures live in store._recent_inbox_states (process memory)."""
    store, account, reader = native
    now = _fake_clock(monkeypatch)
    monkeypatch.setattr(reader, "chats", lambda **kw: ChatPage([BAD], None, {BAD: 500}))
    calls = [0]

    def messages(**kw):
        calls[0] += 1
        raise NativeProtocolError("synthetic permanently malformed conversation")

    monkeypatch.setattr(reader, "messages", messages)
    for restart in range(6):  # six worker restarts, 5 simulated minutes each
        store._recent_inbox_states = {}
        for _ in range(30):
            poll_recent(store, TENANT, account, reader, connection_revision=1, clock=lambda: now[0])
            now[0] += INTERVAL_SECONDS
    assert calls[0] <= MAX_ATTEMPTS, (
        f"inv.14: permanently failing fan read {calls[0]}x over 30 min with 6 restarts "
        f"(expected <= {MAX_ATTEMPTS} total, then a durable visible give-up)")
