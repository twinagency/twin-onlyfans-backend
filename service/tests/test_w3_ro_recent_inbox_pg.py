"""W3 recent_inbox failed fans (inv.14, inv.7): no pin, backoff not park, fences
untouched. Synthetic PG only; reader I/O is monkeypatched, no network, no sends."""
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database  # noqa: F401 (fixture)
from test_onlyfans_launch_provider_budget_pg import native  # noqa: F401 (fixture)
from test_onlyfans_connections_pg import TENANT
from db.models import Message, OfConversationState
from db.onlyfans_connection_models import OfProviderConnection
from onlyfans_runtime.store import Event, Refused
from onlyfans_runtime.worker import ChatPage, MessagePage
from onlyfans_runtime.recent_inbox import poll_recent, INTERVAL_SECONDS
from onlyfans_runtime.native_protocol import NativeProtocolError
import onlyfans_runtime.provider_read_budget as budget

BAD, GOOD = 911, 912
MAX_READS_PER_30_MIN = 6  # 60 s doubling backoff: reads at 0, 60, 180, 420, 900 s
RECOVERY_SECONDS = 900  # the 5th backoff (960 s, from 900 s) ends 60 s after the 30 min


def _fake_clock(monkeypatch, start=100.0):
    now = [start]
    wall0 = budget.utcnow()
    # The shared read budget refills on wall time; drive it from the fake clock.
    monkeypatch.setattr(budget, "utcnow", lambda: wall0 + timedelta(seconds=now[0]))
    return now


def _event(fan, mid, body=None):
    return Event(fan, mid, datetime.now(timezone.utc).isoformat(), "in", body or f"Synthetic message {mid}")


def _poll(store, account, reader, now):
    return poll_recent(store, TENANT, account, reader, connection_revision=1, clock=lambda: now[0])


@pytest.mark.parametrize("bad_page", [
    lambda: MessagePage([_event(GOOD, 300)], False),           # rows of another fan (_commit check)
    lambda: MessagePage([_event(BAD, 300, "x" * 20001)], False),  # Event.normalized refusal
], ids=["other-fan-rows", "oversized-body"])
def test_fan_specific_refusal_does_not_pin_the_inbox(native, monkeypatch, bad_page):
    store, account, reader = native
    now = _fake_clock(monkeypatch)
    monkeypatch.setattr(reader, "chats", lambda **kw: ChatPage([BAD, GOOD], None, {BAD: 300, GOOD: 400}))
    calls = {BAD: 0, GOOD: 0}

    def messages(**kw):
        calls[kw["fan_id"]] += 1
        return bad_page() if kw["fan_id"] == BAD else MessagePage([_event(GOOD, 400)], False)

    monkeypatch.setattr(reader, "messages", messages)
    for _ in range(120):  # inbox_worker re-polls the account every second
        _poll(store, account, reader, now)
        now[0] += 1
    with Session(store.engine) as s:
        imported = s.get(Message, (account, GOOD, 400)) is not None
    assert imported, f"inv.7/14: refused conversation pinned the inbox: BAD {calls[BAD]}x, GOOD {calls[GOOD]}x"
    assert calls[BAD] <= 3, f"refused fan re-read {calls[BAD]}x in 120 s"


def test_missing_head_backs_off_and_is_read_again_after_recovery(native, monkeypatch):
    store, account, reader = native
    now = _fake_clock(monkeypatch)
    monkeypatch.setattr(reader, "chats", lambda **kw: ChatPage([BAD], None, {}))  # no head hint
    broken, calls = [True], [0]

    def messages(**kw):
        calls[0] += 1
        if broken[0]:
            raise NativeProtocolError("synthetic transient conversation failure")
        return MessagePage([_event(BAD, 500)], False)

    monkeypatch.setattr(reader, "messages", messages)
    for _ in range(1800 // INTERVAL_SECONDS):
        _poll(store, account, reader, now)
        now[0] += INTERVAL_SECONDS
    state = store._recent_inbox_states[(TENANT, account, 1)]
    assert 1 <= calls[0] <= MAX_READS_PER_30_MIN and state.backed_off(now[0]) == 1
    broken[0] = False
    with Session(store.engine) as s:
        for _ in range(RECOVERY_SECONDS // INTERVAL_SECONDS):  # backoff expires; no park
            if s.get(Message, (account, BAD, 500)) is not None:
                break
            now[0] += INTERVAL_SECONDS
            _poll(store, account, reader, now)
        assert s.get(Message, (account, BAD, 500)) is not None, "recovered fan without a head hint never re-read"
    assert state.backed_off(now[0]) == 0 and not state.failed


def test_all_skipped_page_records_skipped_input(native, monkeypatch):
    """Only a tip above the stored head: no events, has_more, next_before set (as the connector returns)."""
    store, account, reader = native
    now, head = _fake_clock(monkeypatch), [300]
    monkeypatch.setattr(reader, "chats", lambda **kw: ChatPage([BAD], None, {BAD: head[0]}))
    monkeypatch.setattr(reader, "messages", lambda **kw: MessagePage([_event(BAD, 300)], False) if head[0] == 300
                        else MessagePage([], True, next_before=305, skipped_inbound=(305,)))
    _poll(store, account, reader, now)
    head[0], now[0] = 305, now[0] + INTERVAL_SECONDS
    _poll(store, account, reader, now)
    with Session(store.engine) as s:
        assert s.get(OfConversationState, (account, BAD)).skipped_input_id == 305, "all-skipped page refused"


def test_connection_fence_is_not_a_fan_failure(native, monkeypatch):
    store, account, reader = native
    now = _fake_clock(monkeypatch)
    monkeypatch.setattr(reader, "chats", lambda **kw: ChatPage([BAD], None, {BAD: 300}))

    def messages(**kw):  # reconnect between the head read and the commit
        with Session(store.engine) as s, s.begin():
            s.get(OfProviderConnection, account).revision += 1
        return MessagePage([_event(BAD, 300)], False)
    monkeypatch.setattr(reader, "messages", messages)
    with pytest.raises(Refused):
        _poll(store, account, reader, now)
    assert store._recent_inbox_states[(TENANT, account, 1)].failures == 0


@pytest.mark.xfail(strict=True, reason="G03b: per-fan backoff is process memory "
                   "(HeadState.failed); restart durability needs a schema column")
def test_failed_fan_backoff_survives_worker_restart(native, monkeypatch):
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
            _poll(store, account, reader, now)
            now[0] += INTERVAL_SECONDS
    assert calls[0] <= MAX_READS_PER_30_MIN, f"inv.14: failing fan read {calls[0]}x over 30 min with 6 restarts"
