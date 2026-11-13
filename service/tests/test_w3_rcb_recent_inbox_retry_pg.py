"""W3 recent_inbox (inv.14): a failing fan's read rate is bounded by capped backoff,
never parked; holds past MAX_QUEUED_FANS. Synthetic only; no network."""
from collections import Counter
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database  # noqa: F401 (fixture)
from test_onlyfans_launch_provider_budget_pg import native  # noqa: F401 (fixture)
from test_onlyfans_connections_pg import TENANT
from db.models import Message
from onlyfans_runtime.store import Event
from onlyfans_runtime.worker import ChatPage, MessagePage
import onlyfans_runtime.recent_inbox as recent_inbox
from onlyfans_runtime.recent_inbox import poll_recent, INTERVAL_SECONDS, MAX_QUEUED_FANS
from onlyfans_runtime.native_protocol import NativeProtocolError
import onlyfans_runtime.provider_read_budget as budget

BAD, GOOD = 901, 902
SIM_SECONDS = 30 * 60
MAX_READS_PER_30_MIN = 6  # 60 s doubling backoff: reads at 0, 60, 180, 420, 900 s
RECOVERY_SECONDS = 900  # the 5th backoff (960 s, from 900 s) ends 60 s after the 30 min


def _message(fan, mid):
    return MessagePage([Event(fan, mid, datetime.now(timezone.utc).isoformat(), "in", f"Synthetic message {mid}")], False)


def test_failing_fan_backs_off_while_other_fans_import_then_recovers(native, monkeypatch):
    store, account, reader = native
    now = [100.0]
    good_head, broken = [500], [True]
    calls = {BAD: 0, GOOD: 0}
    # The shared read budget refills on wall time; advance it with the fake clock.
    wall0 = budget.utcnow()
    monkeypatch.setattr(budget, "utcnow", lambda: wall0 + timedelta(seconds=now[0]))
    # GOOD gets a new message every minute; BAD's head never changes.
    monkeypatch.setattr(reader, "chats",
                        lambda **kw: ChatPage([BAD, GOOD], None, {BAD: 200, GOOD: good_head[0]}))

    def messages(**kw):
        calls[kw["fan_id"]] += 1
        if kw["fan_id"] == BAD:
            if broken[0]:
                raise NativeProtocolError("synthetic malformed conversation")
            return _message(BAD, 200)
        return _message(GOOD, good_head[0])

    monkeypatch.setattr(reader, "messages", messages)
    expected_good, start = [], now[0]
    while now[0] - start < SIM_SECONDS:
        if int(now[0] - start) % 60 == 0:
            good_head[0] += 1
            expected_good.append(good_head[0])
        poll_recent(store, TENANT, account, reader, connection_revision=1, clock=lambda: now[0])
        now[0] += INTERVAL_SECONDS
    with Session(store.engine) as s:
        missing = [mid for mid in expected_good if s.get(Message, (account, GOOD, mid)) is None]
    assert not missing, f"healthy fan lost messages {missing}"
    assert calls[BAD] <= MAX_READS_PER_30_MIN, f"inv.14: failing fan read {calls[BAD]}x in {SIM_SECONDS}s"
    # Same head, provider recovered: the backoff expires, it is not a park.
    broken[0] = False
    with Session(store.engine) as s:
        for _ in range(RECOVERY_SECONDS // INTERVAL_SECONDS):
            if s.get(Message, (account, BAD, 200)) is not None:
                break
            now[0] += INTERVAL_SECONDS
            poll_recent(store, TENANT, account, reader, connection_revision=1, clock=lambda: now[0])
        assert s.get(Message, (account, BAD, 200)) is not None, "recovered fan never read again"


def test_backoff_bound_holds_for_more_failing_fans_than_the_queue(monkeypatch):
    fans_total = MAX_QUEUED_FANS + 500
    calls = Counter()
    # DB-free: provider reads go straight to the fake reader.
    monkeypatch.setattr(recent_inbox, "_read", lambda store, tenant, account, revision, callback, consume:
                        (lambda page: (list(page.fan_ids), page.heads, page.next_cursor))(callback()))

    def chats(cursor, limit):
        k = int(cursor or 0)
        fans = list(range(k * limit + 1, min(fans_total, (k + 1) * limit) + 1))
        return ChatPage(fans, str(k + 1) if (k + 1) * limit < fans_total else None, {f: 7 for f in fans})

    def messages(fan_id, **kw):
        calls[fan_id] += 1
        raise NativeProtocolError("synthetic malformed conversation")

    reader = SimpleNamespace(account_id="of-bound", chats=chats, messages=messages)
    store, now = SimpleNamespace(), [0.0]
    for _ in range(2 * SIM_SECONDS):  # one poll per second for an hour
        poll_recent(store, "tenant", "of-bound", reader, connection_revision=1, clock=lambda: now[0])
        now[0] += 1
    assert len(calls) == fans_total, "every failing fan is still served"
    # Doubling from 60 s: reads at 0, 60, 180, 420, 900, 1860 s within the hour.
    assert max(calls.values()) <= 6, f"backoff lost under eviction: a fan was read {max(calls.values())}x in 1 h"
