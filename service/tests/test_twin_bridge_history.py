#!/usr/bin/env python3
"""Offline provider-history coverage regressions; no sessions or network calls.

The production adapter receives an explicit fake provider client. Database
configuration is isolated in memory, dotenv loading is disabled, and no database
connection is opened. All page responses and identities are synthetic.
"""
from __future__ import annotations

import asyncio
import os
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from twin_bridge import platform  # noqa: E402


ACCOUNT = "history-test-account"
FAN = 9001
BOUNDARY = datetime(2026, 10, 16, 1, 30, 17)
checks = 0


def check(label, got, want):
    global checks
    checks += 1
    assert got == want, f"{label}: got {got!r}, want {want!r}"
    print(f"  ok   {label}")


def message(mid, seconds=0, *, outbound=False):
    instant = BOUNDARY + timedelta(seconds=seconds)
    return {
        "id": mid,
        "createdAt": instant.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "isFromMe": outbound,
        "fromUser": {"id": 8001 if outbound else FAN},
    }


def page(*rows, more=False):
    return {"list": list(rows), "hasMore": more}


class FakeClient:
    account_id = ACCOUNT
    platform = "onlyfans"

    def __init__(self, pages):
        self.pages = deepcopy(pages)
        self.calls = []

    def get_messages(self, chat_id, **kwargs):
        self.calls.append((chat_id, kwargs))
        if not self.pages:
            raise AssertionError("unexpected extra history request")
        result = self.pages.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def send_text_once_body(self, *_args, **_kwargs):
        raise AssertionError("history tests must never send")


async def read(pages, *, source_ids=None, limit=10, since=BOUNDARY):
    fake = FakeClient(pages)
    adapter = platform.OnlyFansTextAdapter(ACCOUNT, client=fake, history_limit=limit)
    rows = await adapter.preflight_history(
        FAN, since=since, source_ids={100} if source_ids is None else source_ids,
    )
    return rows, fake


async def refused(label, pages, *, source_ids=None, limit=10, since=BOUNDARY,
                  detail=None, expected_calls=None):
    fake = FakeClient(pages)
    adapter = platform.OnlyFansTextAdapter(ACCOUNT, client=fake, history_limit=limit)
    try:
        await adapter.preflight_history(
            FAN, since=since, source_ids={100} if source_ids is None else source_ids,
        )
    except platform.PlatformError as exc:
        check(label, exc.reason, "history-unreadable")
        if detail is not None:
            check(f"{label}: fixed detail", exc.detail, detail)
    else:
        raise AssertionError(f"{label}: unexpectedly accepted")
    if expected_calls is not None:
        check(f"{label}: bounded calls", len(fake.calls), expected_calls)


async def main():
    rows, fake = await read([
        page(message(100), message(99, -10, outbound=True), message(98, -20, outbound=True)),
    ])
    check("historical page retains all rows for caller classification", [r["id"] for r in rows], [100, 99, 98])
    check("single covered page reads once", len(fake.calls), 1)
    check("first request is descending without a cursor", fake.calls[0], (FAN, {"limit": 10, "order": "desc"}))

    rows, fake = await read([
        page(message(103, 3), more=True),
        page(message(102, 2, outbound=True), message(100), more=True),
        page(message(99, -1)),
    ])
    check("newer human on second page remains visible", [r["id"] for r in rows], [103, 102, 100, 99])
    check("short pages with hasMore true continue", len(fake.calls), 3)
    check("second request uses last row's older cursor", fake.calls[1][1]["before_id"], 103)
    check("third request advances cursor", fake.calls[2][1]["before_id"], 100)

    rows, fake = await read([
        page(message(101), message(100), more=True),
        page(message(99, outbound=True), more=True),
        page(message(98, -1)),
    ])
    check("timestamp ties do not prove boundary", len(fake.calls), 3)
    check("equal-time creator message stays visible", rows[2]["isFromMe"], True)
    rows, fake = await read([
        page(message(100), more=True),
        page(message(99, outbound=True), message(98, -1)),
    ], since=BOUNDARY.replace(microsecond=999))
    check("submillisecond source boundary does not stop on same-millisecond row", len(fake.calls), 2)
    check("same-millisecond outbound stays covered", rows[1]["isFromMe"], True)
    rows, fake = await read([page(message(100), more=False)])
    check("explicit exhaustion covers equal boundary", len(rows), 1)
    check("explicit exhaustion does not add a read", len(fake.calls), 1)

    rows, _ = await read([{"messages": [message(100), message(99, -1)]}])
    check("older boundary allows absent hasMore and messages alias", len(rows), 2)
    rows, _ = await read([page(message(100), message(99, -1), more="unneeded")])
    check("older boundary proves coverage without trusting malformed hasMore", len(rows), 2)
    rows, _ = await read([page(message(101, 1), message(100), more=False)], source_ids={100, 101})
    check("all frozen source ids observed", len(rows), 2)
    row = message("100")
    row["createdAt"] = "2026-10-16T11:30:17+10:00"
    rows, _ = await read([page(row)])
    check("decimal id and offset timestamp are accepted unchanged", rows[0], row)

    await refused("missing source at exhaustion", [page(message(99, -1))], detail="source-message-missing")
    await refused("one of multiple source ids missing", [page(message(100), message(99, -1))], source_ids={100, 101})
    await refused("crossed boundary cannot excuse missing source", [page(message(99, -1), more=True)], expected_calls=1)
    await refused("empty exhausted history misses source", [page()], detail="source-message-missing")
    await refused("empty continuing page has no progress", [page(more=True)], detail="history-cursor-no-progress")
    await refused("empty page after observed source without exhaustion", [page(message(100), more=True), page(more=True)], expected_calls=2)

    for flag in (None, 0, 1, "false", "true", [], {}):
        await refused(f"nonboolean pagination flag {flag!r}", [page(message(100), more=flag)], detail="invalid-has-more")
    await refused("missing pagination flag before boundary", [{"list": [message(100)]}], detail="invalid-has-more")

    for stamp in (None, "", "2026-10-16", "2026-10-16T01:30:17", "2026-02-30T00:00:00Z", "0000-01-01T00:00:00Z", "0001-01-01T00:00:00+14:00", True, 123):
        row = message(100)
        row["createdAt"] = stamp
        await refused(f"unreadable timestamp {stamp!r}", [page(row)], detail="invalid-message-time")
    row = message(100)
    del row["createdAt"]
    await refused("absent timestamp", [page(row)], detail="invalid-message-time")

    for mid in (None, True, False, 0, -1, 100.5, "100.5", "1e2", " 100", "100 ", {}, [], "", "100\n"):
        await refused(f"malformed message id {mid!r}", [page(message(mid))], detail="invalid-message-id")
    await refused("duplicate within page", [page(message(100), message("100"))], detail="history-cursor-no-progress")
    await refused("repeated cursor across pages", [page(message(100), more=True), page(message(100))], detail="history-cursor-no-progress", expected_calls=2)
    await refused("cursor moves toward newer ids", [page(message(100), more=True), page(message(101))], detail="history-cursor-no-progress")
    await refused("time rises within page", [page(message(100), message(99, 1))], detail="history-out-of-order")
    await refused("time rises across pages", [page(message(101, 1), more=True), page(message(100, 2))], detail="history-out-of-order")

    for invalid in (None, {}, {"list": {}}, {"list": [None]}, {"error": "synthetic", "list": []}):
        await refused(f"malformed envelope {invalid!r}", [invalid])
    await refused("provider error becomes fixed reason", [RuntimeError("synthetic private detail")], detail="RuntimeError")
    await refused("empty source set", [], source_ids=set(), expected_calls=0)
    await refused("boolean source id", [], source_ids={True}, expected_calls=0)
    await refused("negative source id", [], source_ids={-1}, expected_calls=0)
    await refused("non-set source ids", [], source_ids=[100], expected_calls=0)
    await refused("missing boundary", [], since=None, expected_calls=0)
    await refused("aware boundary refused", [], since=BOUNDARY.replace(tzinfo=timezone.utc), expected_calls=0)
    await refused("non-datetime boundary refused", [], since="2026-10-16T01:30:17Z", expected_calls=0)
    await refused("invalid page limit", [], limit=0, expected_calls=0)

    pages = [page(message(2000 - i, 1), more=True) for i in range(platform.HISTORY_MAX_PAGES)]
    await refused("page cap refuses incomplete coverage", pages, source_ids={2000}, detail="history-page-cap", expected_calls=20)
    pages[-1] = page(message(100, -1))
    rows, fake = await read(pages, source_ids={2000})
    check("boundary on last allowed page is accepted", len(fake.calls), 20)
    check("all capped walk rows returned", len(rows), 20)

    thousand = [message(2000 - i, 1) for i in range(platform.HISTORY_MAX_ITEMS)]
    await refused("item cap refuses incomplete coverage", [page(*thousand, more=True)], source_ids={2000}, limit=1000, detail="history-item-cap", expected_calls=1)
    rows, fake = await read([page(*thousand)], source_ids={2000}, limit=5000)
    check("explicit exhaustion at item cap is accepted", len(rows), 1000)
    check("requested page size is bounded", fake.calls[0][1]["limit"], 1000)
    await refused("oversized returned page refused", [page(*(thousand + [message(999, -1)]))], source_ids={2000}, detail="history-item-cap", expected_calls=1)

    fake = FakeClient([{"list": [{"legacy": "no ids or timestamps required"}]}])
    adapter = platform.OnlyFansTextAdapter(ACCOUNT, client=fake)
    rows = await adapter.preflight_history(FAN)
    check("unscoped legacy validation remains unchanged", rows, [{"legacy": "no ids or timestamps required"}])
    check("unscoped legacy call is one page", len(fake.calls), 1)


if __name__ == "__main__":
    asyncio.run(main())
    print(f"\n{checks} checks; all cases passed")
