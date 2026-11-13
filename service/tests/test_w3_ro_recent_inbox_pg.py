"""W3 recent_inbox failed fans (inv.14, inv.7; W3RO-01/02, W3RCB-03): no pin, capped backoff
not park, bound holds past MAX_QUEUED_FANS, fences untouched. Synthetic PG only; reader I/O is
monkeypatched, no network, no sends."""
from collections import Counter
from datetime import datetime, timezone, timedelta
from itertools import accumulate
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database  # noqa: F401 (fixture)
from test_onlyfans_launch_provider_budget_pg import native  # noqa: F401 (fixture)
from test_onlyfans_connections_pg import TENANT
from db.models import Message, OfConversationState
from db.onlyfans_connection_models import OfProviderConnection
from onlyfans_runtime.store import Event, Refused
from onlyfans_runtime.worker import ChatPage, MessagePage
import onlyfans_runtime.recent_inbox as recent_inbox
from onlyfans_runtime.recent_inbox import (poll_recent, INTERVAL_SECONDS, MAX_QUEUED_FANS,
                                           FAILED_FAN_RETRY_SECONDS, MAX_FAN_BACKOFF_SECONDS)
from onlyfans_runtime.native_protocol import NativeProtocolError
import onlyfans_runtime.provider_read_budget as budget

BAD, GOOD = 911, 912
# An always-failing fan is read at 0, 60, 180, 420, 900, 1860, ... s (60 s doubling, capped).
READ_TIMES = list(accumulate([0] + [min(FAILED_FAN_RETRY_SECONDS * 2 ** n, MAX_FAN_BACKOFF_SECONDS) for n in range(10)]))
RECOVERY_SECONDS = 900  # the 5th backoff (960 s, from 900 s) ends 60 s after the 30 min


def max_reads(seconds):
    return sum(t < seconds for t in READ_TIMES)


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


def _run(store, account, reader, now, seconds, step=INTERVAL_SECONDS, tick=lambda t: None):
    for t in range(0, seconds, step):
        tick(t)
        _poll(store, account, reader, now)
        now[0] += step


def _recovers(store, account, reader, now, fan, mid):
    """Provider fixed, same head: the backoff expires (no park) and the fan is read again."""
    with Session(store.engine) as s:
        for _ in range(RECOVERY_SECONDS // INTERVAL_SECONDS):
            if s.get(Message, (account, fan, mid)) is not None:
                return True
            now[0] += INTERVAL_SECONDS
            _poll(store, account, reader, now)
        return s.get(Message, (account, fan, mid)) is not None


@pytest.mark.parametrize("bad_page", [
    lambda: MessagePage([_event(GOOD, 300)], False),           # rows of another fan (_commit check)
    lambda: MessagePage([_event(BAD, 300, "x" * 20001)], False),  # Event.normalized refusal
], ids=["other-fan-rows", "oversized-body"])
def test_fan_specific_refusal_does_not_pin_the_inbox(native, monkeypatch, bad_page):
    store, account, reader = native
    now = _fake_clock(monkeypatch)
    monkeypatch.setattr(reader, "chats", lambda **kw: ChatPage([BAD, GOOD], None, {BAD: 300, GOOD: 400}))
    calls = Counter()

    def messages(fan_id, **kw):
        calls[fan_id] += 1
        return bad_page() if fan_id == BAD else MessagePage([_event(GOOD, 400)], False)

    monkeypatch.setattr(reader, "messages", messages)
    _run(store, account, reader, now, 120, step=1)  # inbox_worker re-polls the account every second
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
    _run(store, account, reader, now, 1800)
    state = store._recent_inbox_states[(TENANT, account, 1)]
    assert 1 <= calls[0] <= max_reads(1800) and state.backed_off(now[0]) == 1
    broken[0] = False
    assert _recovers(store, account, reader, now, BAD, 500), "recovered fan without a head hint never re-read"
    assert state.backed_off(now[0]) == 0 and not state.failed


def test_failing_fan_backs_off_while_other_fans_import_then_recovers(native, monkeypatch):
    store, account, reader = native
    now = _fake_clock(monkeypatch)
    good_head, broken, calls, expected_good = [500], [True], Counter(), []
    # GOOD gets a new message every minute; BAD's head never changes.
    monkeypatch.setattr(reader, "chats", lambda **kw: ChatPage([BAD, GOOD], None, {BAD: 200, GOOD: good_head[0]}))

    def messages(fan_id, **kw):
        calls[fan_id] += 1
        if fan_id == BAD and broken[0]:
            raise NativeProtocolError("synthetic malformed conversation")
        return MessagePage([_event(fan_id, 200 if fan_id == BAD else good_head[0])], False)

    def tick(t):
        if t % 60 == 0:
            good_head[0] += 1
            expected_good.append(good_head[0])

    monkeypatch.setattr(reader, "messages", messages)
    _run(store, account, reader, now, 1800, tick=tick)
    with Session(store.engine) as s:
        missing = [mid for mid in expected_good if s.get(Message, (account, GOOD, mid)) is None]
    assert not missing, f"healthy fan lost messages {missing}"
    assert calls[BAD] <= max_reads(1800), f"inv.14: failing fan read {calls[BAD]}x in 30 min"
    broken[0] = False
    assert _recovers(store, account, reader, now, BAD, 200), "recovered fan never read again"


def test_backoff_bound_holds_for_more_failing_fans_than_the_queue(monkeypatch):
    fans_total, calls = MAX_QUEUED_FANS + 500, Counter()
    # DB-free and unbudgeted: the real consume runs, and every listed fan counts as missing.
    monkeypatch.setattr(recent_inbox, "_read", lambda store, tenant, account, revision, read, consume: consume(read(), None))
    monkeypatch.setattr(recent_inbox, "_missing", lambda store, tenant, account, revision, page, claim: list(page.fan_ids))

    def chats(cursor, limit):
        k = int(cursor or 0)
        fans = list(range(k * limit + 1, min(fans_total, (k + 1) * limit) + 1))
        return ChatPage(fans, str(k + 1) if (k + 1) * limit < fans_total else None, {f: 7 for f in fans})

    def messages(fan_id, **kw):
        calls[fan_id] += 1
        raise NativeProtocolError("synthetic malformed conversation")

    reader = SimpleNamespace(account_id="of-bound", chats=chats, messages=messages)
    store, now = SimpleNamespace(), [0.0]
    for _ in range(3600):  # one poll per second for an hour
        poll_recent(store, "tenant", "of-bound", reader, connection_revision=1, clock=lambda: now[0])
        now[0] += 1
    assert len(calls) == fans_total, "every failing fan is still served"
    assert max(calls.values()) <= max_reads(3600), f"backoff lost under eviction: a fan was read {max(calls.values())}x in 1 h"


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


@pytest.mark.parametrize("has_more", [False, True])
def test_covered_skipped_head_is_read_once_until_it_changes(native, monkeypatch, has_more):
    """Tip-only head, fan without conversation state: _skipped_input stores nothing, so only memory stops re-reads."""
    store, account, reader = native
    now, head, calls = _fake_clock(monkeypatch), [305], [0]
    monkeypatch.setattr(reader, "chats", lambda **kw: ChatPage([BAD], None, {BAD: head[0]}))
    monkeypatch.setattr(reader, "messages", lambda **kw: calls.__setitem__(0, calls[0] + 1) or MessagePage(
        [], has_more, next_before=head[0], skipped_inbound=(head[0],)))
    _run(store, account, reader, now, 600)
    assert calls[0] == 1, f"covered tip-only head re-read {calls[0]}x in 10 min"
    assert store._recent_inbox_states[(TENANT, account, 1)].backed_off(now[0]) == 0, "a settled head is not a backed-off failure"
    head[0] = 306
    _run(store, account, reader, now, 60)
    assert calls[0] == 2, "a new head is read again"


@pytest.mark.parametrize("page", [
    lambda: MessagePage([_event(BAD, 300)], True),  # more below, legacy cursor: 305 not provably covered
    lambda: MessagePage([], True, next_before=310, skipped_inbound=(310,)),  # cursor above the head
    lambda: MessagePage([], False),  # no rows at all: proves nothing about the head
], ids=["cursor-none", "cursor-above-head", "empty-page"])
def test_uncovered_head_is_not_settled(native, monkeypatch, page):
    store, account, reader = native
    now, calls = _fake_clock(monkeypatch), [0]
    monkeypatch.setattr(reader, "chats", lambda **kw: ChatPage([BAD], None, {BAD: 305}))
    monkeypatch.setattr(reader, "messages", lambda **kw: calls.__setitem__(0, calls[0] + 1) or page())
    _run(store, account, reader, now, 60)
    assert calls[0] > 1, "uncovered head settled"
