"""Provider-reported period totals: parsing, windows, refresh cadence, fenced storage and the read model."""
import asyncio
import json
import threading
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from test_onlyfans_connections_pg import verified, TENANT
from db.onlyfans_earnings_total_models import OfEarningsTotal
from onlyfans_read.earnings import read_earnings
from onlyfans_runtime import earnings_totals as et, provider_read_budget
from onlyfans_runtime.provider_connector import NativeProviderConnector
from onlyfans_runtime.store import utcnow


def chart(gross="3415.6", net="2732.37"):
    return json.dumps({"total": {"delta": -54, "chartAmount": [{"date": "2026-10-02T00:00:00+00:00", "count": 132.69}],
                                 "total": float(net), "gross": float(gross)}}).encode()


def test_parse_chart_reads_exact_cents_and_refuses_anything_unproven():
    assert et.parse_chart(chart()) == (341560, 273237)
    assert et.parse_chart(chart("0", "0")) == (0, 0)
    assert et.parse_chart(chart("812294.48", "649825.51")) == (81229448, 64982551)
    for bad in (b"[]", b"", b"not json", b'{"total": {}}', b'{"total": {"gross": 5}}',
                b'{"total": {"gross": "5.00", "total": "4.00"}}',    # strings are not numbers
                b'{"total": {"gross": true, "total": 1}}',
                b'{"total": {"gross": 5, "total": 6}}',               # net above gross
                b'{"total": {"gross": -1, "total": -2}}',
                b'{"total": {"gross": NaN, "total": 1}}',
                b'{"total": {"gross": 1e30, "total": 1}}'):
        with pytest.raises(ValueError):
            et.parse_chart(bad)


def test_period_windows_are_utc_day_aligned_and_lifetime_starts_early():
    now = datetime(2026, 11, 1, 13, 45, 12)
    assert et.period_window("7d", now) == (datetime(2026, 10, 26), now)
    assert et.period_window("30d", now) == (datetime(2026, 10, 3), now)
    assert et.period_window("90d", now) == (datetime(2026, 8, 4), now)
    assert et.period_window("all", now) == (datetime(2016, 1, 1), now)


def test_connector_allows_only_the_exact_chart_read_and_signs_a_bounded_window(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    from test_onlyfans_provider_adapters import credential
    wire = NativeProviderConnector(credential())
    monkeypatch.setattr(wire, "verify_identity", lambda: None)
    seen = []
    monkeypatch.setattr(wire, "_call", lambda method, path: seen.append((method, path)) or chart())
    wire.earnings_chart(start=datetime(2026, 10, 3), end=datetime(2026, 11, 1, 1))
    method, path = seen[0]
    assert method == "GET" and path.startswith("/api2/v2/earnings/chart?")
    assert "startDate=2026-10-03+00%3A00%3A00" in path and "withTotal=true" in path and "filter%5Btotal_amount%5D=total_amount" in path
    for bad in ({"start": datetime(2026, 11, 2), "end": datetime(2026, 11, 1)}, {"start": datetime(2000, 1, 1), "end": datetime(2026, 11, 1)}):
        with pytest.raises(Exception):
            wire.earnings_chart(**bad)
    store.close()


def test_connector_refuses_other_earnings_paths_and_writes(database, monkeypatch):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    from test_onlyfans_provider_adapters import credential
    wire = NativeProviderConnector(credential())
    for method, path in (("GET", "/api2/v2/earnings"), ("GET", "/api2/v2/earnings/chart/../users/me"),
                         ("POST", "/api2/v2/earnings/chart?startDate=x")):
        with pytest.raises(Exception):
            wire._call(method, path)


def test_due_periods_orders_overdue_first_and_honours_fence_and_retry():
    now = utcnow()
    class Row:
        def __init__(self, age, binding="b", revision=1): self.fetched_at, self.binding_id, self.connection_revision = now - age, binding, revision
    assert et.due_periods({}, "b", 1, now) == ["7d", "30d", "90d", "all"]
    rows = {"7d": Row(timedelta(minutes=2)), "30d": Row(timedelta(minutes=30)), "90d": Row(timedelta(minutes=19)), "all": Row(timedelta(minutes=70))}
    assert et.due_periods(rows, "b", 1, now) == ["all", "30d"]     # 7d and 90d are fresh
    assert et.due_periods(rows, "b", 2, now) == ["7d", "30d", "90d", "all"]   # a new connection revision invalidates every row
    assert et.due_periods(rows, "other", 1, now) == ["7d", "30d", "90d", "all"]
    assert et.due_periods(rows, "b", 1, now, {"all": now + timedelta(minutes=5)}) == ["30d"]


def test_refresh_stores_one_fenced_row_per_period_and_replaces_it(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    calls = []
    def fake(self, *, start, end):
        calls.append((start, end)); return chart("100.00", "80.00") if len(calls) == 1 else chart("150.50", "120.40")
    monkeypatch.setattr(NativeProviderConnector, "earnings_chart", fake)
    assert et.refresh_one(store, TENANT, account, "30d") is True
    with Session(store.engine) as s:
        row = s.get(OfEarningsTotal, (account, "30d"))
        assert (row.gross_cents, row.net_cents, row.connection_revision) == (10000, 8000, 1)
        assert row.window_end - row.window_start >= timedelta(days=29)
    assert et.refresh_one(store, TENANT, account, "30d") is True
    with Session(store.engine) as s:
        rows = list(s.scalars(select(OfEarningsTotal)))
        assert [(r.period, r.gross_cents, r.net_cents) for r in rows] == [("30d", 15050, 12040)]
        # The provider lease was released: another read can start immediately.
        provider_read_budget.release(store, TENANT, provider_read_budget.acquire(store, TENANT, account, connection_revision=1, kind="ledger"))
    store.close()


def test_failed_or_unparseable_reads_store_nothing_and_release_the_lease(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    monkeypatch.setattr(NativeProviderConnector, "earnings_chart", lambda self, **kw: b"[]")
    with pytest.raises(ValueError):
        et.refresh_one(store, TENANT, account, "7d")
    with Session(store.engine) as s:
        assert s.scalars(select(OfEarningsTotal)).first() is None
    provider_read_budget.release(store, TENANT, provider_read_budget.acquire(store, TENANT, account, connection_revision=1, kind="ledger"))
    store.close()


def test_worker_tick_reads_one_period_per_account_and_backs_off_failures(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    done, seen = [], []
    def refresh(store_, tenant, acct, period):
        done.append(period)
        if period == "30d": raise RuntimeError("provider down")
    worker = et.EarningsTotalsWorker(store, TENANT, threading.Event(), observe=seen.append, refresh=refresh)
    worker.tick(); worker.tick()
    assert done == ["7d", "7d"]                      # nothing stored by the fake, so the same period stays due
    done.clear()
    worker.retry_after[(account, "7d")] = utcnow() + timedelta(minutes=5)
    worker.tick(); worker.tick()
    assert done == ["30d", "90d"] and worker.retry_after[(account, "30d")] > utcnow()   # 30d failed once and waits, so the next tick moves on to 90d
    assert seen and seen[0]["state"] == "earnings_totals_unavailable"
    store.close()


def read(database, **kw):
    now = utcnow()
    async def run():
        engine = create_async_engine(make_url(database[0]).set(drivername="postgresql+asyncpg"))
        try:
            async with async_sessionmaker(engine)() as session:
                async with session.begin():
                    return await read_earnings(session, tenant_id=TENANT, start=(now - timedelta(days=30)).isoformat(timespec="milliseconds") + "Z",
                                               end=now.isoformat(timespec="milliseconds") + "Z", **kw)
        finally:
            await engine.dispose()
    return asyncio.run(run())


def test_read_model_returns_current_totals_even_while_the_ledger_is_unavailable(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    monkeypatch.setattr(NativeProviderConnector, "earnings_chart", lambda self, **kw: chart("100.00", "80.00"))
    assert read(database)["providerTotals"] == []
    for period in ("7d", "all"):
        et.refresh_one(store, TENANT, account, period)
    result = read(database, include_pending=True, include_history=True)
    assert result["status"] == "unavailable" and result["clearedGrossCents"] is None     # ledger still not proven
    totals = result["providerTotals"]
    assert [(t["accountId"], t["period"], t["grossCents"], t["netCents"]) for t in totals] == [(account, "30d" if False else "7d", 10000, 8000), (account, "all", 10000, 8000)]
    assert all(t["fetchedAt"].endswith("Z") and t["windowStart"] < t["windowEnd"] for t in totals)
    # A rebinding of the account to another creator binding must hide the old numbers.
    with Session(store.engine, expire_on_commit=False) as s, s.begin():
        s.get(OfEarningsTotal, (account, "7d")).binding_id = "some-older-binding"
    assert [t["period"] for t in read(database)["providerTotals"]] == ["all"]
    store.close()
