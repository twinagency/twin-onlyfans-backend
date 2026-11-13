"""LW-A1: an OF 429 on any read reaches the account backoff with its Retry-After.

W7LW-01 (dup-of W3RO-05): a 429 inside preserve_unread was replaced by
UnreadPreservationError, so Retry-After never reached the read budget and the
confirm GET went straight back into the 429. W7LW-08: the wire 429 keeps the
Retry-After value. W7LW-09: the per-fan backoff is capped, never parked.
Synthetic ids/PG only; provider I/O monkeypatched, no network, no sends."""
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database  # noqa: F401 (fixture)
from test_onlyfans_launch_provider_budget_pg import native  # noqa: F401 (fixture)
from test_provider_backoff_classification import transport  # noqa: F401 (fixture)
from test_w3_ro_recent_inbox_pg import _fake_clock, _poll
from db.onlyfans_content_sync_progress_models import OfProviderReadBudget
from onlyfans_runtime import provider_connector as connector
from onlyfans_runtime.recent_inbox import HeadState, _fail, FAILED_FAN_RETRY_SECONDS, MAX_FAN_BACKOFF_SECONDS
from onlyfans_runtime.worker import ChatPage
import onlyfans_runtime.provider_read_budget as budget

FAN = 913
RETRY = 120


@pytest.mark.parametrize("where", ["messages-then-confirm", "restore-delete"])
def test_429_inside_unread_preservation_reaches_the_account_backoff(native, monkeypatch, tmp_path, where):
    store, account, reader = native
    now = _fake_clock(monkeypatch)
    monkeypatch.setenv("ONLYFANS_UNREAD_JOURNAL_DIR", str(tmp_path))
    owner = reader._handle.owner_id
    monkeypatch.setattr(reader, "chats", lambda **kw: ChatPage([FAN], None, {FAN: 300}))
    calls = Counter()

    def wire(method, path, body=b"", **kw):
        route = path.split("?")[0].rsplit("/", 1)[-1] if method == "GET" else "DELETE"
        calls[route] += 1
        if route == "me":
            return json.dumps({"id": owner}).encode()
        if route == str(FAN):  # unread snapshot / confirm
            if calls[route] == 1:
                return json.dumps({"withUser": {"id": FAN}, "unreadMessagesCount": 1}).encode()
            if where == "messages-then-confirm":
                raise connector.NativeRateLimitError(RETRY)
            return json.dumps({"withUser": {"id": FAN}, "unreadMessagesCount": 0}).encode()
        if route == "messages":
            if where == "messages-then-confirm":
                raise connector.NativeRateLimitError(RETRY)
            return json.dumps({"list": [], "hasMore": False}).encode()
        if route == "DELETE":
            raise connector.NativeRateLimitError(RETRY)
        raise AssertionError(f"unexpected OF request {method} {path}")

    monkeypatch.setattr(reader, "_signed_request", wire)
    with pytest.raises(connector.NativeRateLimitError):
        _poll(store, account, reader, now)
    with Session(store.engine) as s:
        until = s.get(OfProviderReadBudget, account).backoff_until
    floor = budget.utcnow() + timedelta(seconds=RETRY - 10)
    assert until is not None and until >= floor, (
        f"inv.14 / W3RO-05: OF 429 ({where}) inside preserve_unread lost its Retry-After "
        f"(account backoff_until={until}); next fan is read straight into the 429 window")
    # Never retry tightly: no OF call after the 429; the pending undo waits for the next read.
    assert calls[str(FAN)] == (1 if where == "messages-then-confirm" else 2)
    assert calls["DELETE"] == (0 if where == "messages-then-confirm" else 1)
    assert len(list(tmp_path.glob("*.pending"))) == 1


def test_wire_429_keeps_the_retry_after_value(transport):
    state, session = transport
    state.status = 429  # fixture answers with retry-after: 17
    with pytest.raises(connector.NativeRateLimitError) as caught:
        connector.NativeProviderConnector(session)._signed_request('GET', '/api2/v2/users/me')
    assert caught.value.retry_after == 17


def test_retry_after_http_date_and_cap():
    stamp = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=90), usegmt=True)
    assert 85 <= connector._rate_limit_seconds(stamp) <= 91
    assert connector._rate_limit_seconds('99999') == 3600
    assert connector.NativeRateLimitError(99999).retry_after == 3600


def test_failing_fan_backoff_doubles_then_caps_never_parks():
    state = HeadState()
    delays = [_fail(state, FAN, 0)[1] for _ in range(20)]
    expected = [min(FAILED_FAN_RETRY_SECONDS * 2 ** n, MAX_FAN_BACKOFF_SECONDS) for n in range(20)]
    assert delays == expected and delays[5:] == [MAX_FAN_BACKOFF_SECONDS] * 15 == [1800] * 15
    assert state.failed[FAN].due == MAX_FAN_BACKOFF_SECONDS
