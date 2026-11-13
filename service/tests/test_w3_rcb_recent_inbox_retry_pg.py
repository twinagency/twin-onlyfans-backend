"""W3 runtime-core-b red test: a permanently failing fan must give up visibly (REVIEW inv.14).

recent_inbox.poll_recent retries a fan whose messages() raises a generic error every
FAILED_FAN_RETRY_SECONDS forever. inv.14: bounded retries, give up visibly after 5.
Synthetic PG only; reader I/O is monkeypatched, no network.
"""
from datetime import datetime, timezone, timedelta

from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database  # noqa: F401 (fixture)
from test_onlyfans_launch_provider_budget_pg import native  # noqa: F401 (fixture)
from test_onlyfans_connections_pg import TENANT
from db.models import Message
from onlyfans_runtime.store import Event
from onlyfans_runtime.worker import ChatPage, MessagePage
from onlyfans_runtime.recent_inbox import poll_recent, INTERVAL_SECONDS
from onlyfans_runtime.native_protocol import NativeProtocolError
import onlyfans_runtime.provider_read_budget as budget

BAD, GOOD = 901, 902
SIM_SECONDS = 30 * 60
MAX_ATTEMPTS = 5


def test_failing_fan_gives_up_after_five_attempts_while_other_fans_keep_importing(native, monkeypatch, capsys):
    store, account, reader = native
    now = [100.0]
    good_head = [500]
    calls = {BAD: 0, GOOD: 0}
    # The shared read budget refills on wall time; advance it with the fake clock so
    # 180 simulated polls are not throttled by real elapsed milliseconds.
    wall0 = budget.utcnow()
    monkeypatch.setattr(budget, "utcnow", lambda: wall0 + timedelta(seconds=now[0]))

    # GOOD gets a new message every minute; BAD's head never changes.
    bad_head = [200]
    monkeypatch.setattr(reader, "chats",
                        lambda **kw: ChatPage([BAD, GOOD], None, {BAD: bad_head[0], GOOD: good_head[0]}))

    def messages(**kw):
        calls[kw["fan_id"]] += 1
        if kw["fan_id"] == BAD:
            raise NativeProtocolError("synthetic permanently malformed conversation")
        return MessagePage([Event(GOOD, good_head[0], datetime.now(timezone.utc).isoformat(),
                                  "in", f"Synthetic message {good_head[0]}")], False)

    monkeypatch.setattr(reader, "messages", messages)
    expected_good = []
    start = now[0]
    while now[0] - start < SIM_SECONDS:
        if int(now[0] - start) % 60 == 0:
            good_head[0] += 1
            expected_good.append(good_head[0])
        poll_recent(store, TENANT, account, reader, connection_revision=1, clock=lambda: now[0])
        now[0] += INTERVAL_SECONDS

    with Session(store.engine) as s:
        missing = [mid for mid in expected_good if s.get(Message, (account, GOOD, mid)) is None]
    assert not missing, f"healthy fan lost messages {missing}"
    assert calls[BAD] <= MAX_ATTEMPTS, (
        f"inv.14: failing fan messages() called {calls[BAD]} times in {SIM_SECONDS}s "
        f"(expected <= {MAX_ATTEMPTS}, then a visible give-up)")
    assert '"conversation_parked"' in capsys.readouterr().out, "give-up must be visible"
    # A newer head on the parked fan is a new message: read it again.
    before = calls[BAD]
    bad_head[0] += 1
    poll_recent(store, TENANT, account, reader, connection_revision=1, clock=lambda: now[0])
    assert calls[BAD] == before + 1, "parked fan not retried after its head changed"
