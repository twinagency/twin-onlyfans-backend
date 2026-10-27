"""The actual operations projection uses UTC regardless of PostgreSQL session zone."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from db.models import AccountAiConfig, GrokDailyCost
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_models import OfRuntime, OfWork
from test_onlyfans_foundation_pg import database, event, seed


@pytest.mark.parametrize("session_zone", ["Etc/GMT+12", "Etc/GMT-14"])
def test_operations_cost_day_and_freshness_use_utc_under_non_utc_session(database, monkeypatch, session_zone):
    from onlyfans_read import repo

    url, engine = database
    store = seed(url, engine)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with Session(engine) as s, s.begin():
        runtime = s.get(OfRuntime, "acc-a")
        runtime.recovery_needed = False
        runtime.last_sync_at = now - timedelta(minutes=1)
        s.add(AccountAiConfig(account_id="acc-a", daily_cost_cap_cents=100))
        s.add(GrokDailyCost(day=now.strftime("%Y-%m-%d"), account_id="acc-a", provider="grok",
                            cost_cents=17, call_count=2))
        s.add(OfProviderConnection(account_id="acc-a", binding_id=runtime.binding_id, owner_id=501,
            revision=1, status="verified", sealed_config="synthetic-clock-projection-only",
            verified_at=now, expires_at=now+timedelta(hours=1), updated_at=now))
    store.record("tenant-a", "acc-a", event())
    store.drain("tenant-a", "acc-a")
    with Session(engine) as s, s.begin():
        work = s.get(OfWork, ("acc-a", 101))
        assert work is not None and work.status == "waiting"
        work.due_at = now - timedelta(seconds=30)

    async def project():
        async_engine = create_async_engine(make_url(url).set(drivername="postgresql+asyncpg"),
            connect_args={"server_settings": {"timezone": session_zone}})

        @asynccontextmanager
        async def session():
            async with async_sessionmaker(async_engine)() as s:
                assert await s.scalar(text("SHOW TIME ZONE")) == session_zone
                yield s

        monkeypatch.setattr(repo, "get_session", session)
        try:
            return await repo.SqlOnlyFansReadRepo().operations(tenant_id="tenant-a", account_id="acc-a")
        finally:
            await async_engine.dispose()

    try:
        status = asyncio.run(project())
        assert status["modelCosts"]["day"] == now.strftime("%Y-%m-%d")
        assert status["modelCosts"]["spentMillicents"] == 17
        assert status["modelCosts"]["calls"] == 2
        assert status["providerConnectionVerified"] is True
        assert not {"history_not_current", "history_sync_time_invalid"}.intersection(status["reasons"])
        assert 25 <= status["work"]["oldestWaitingAgeSeconds"] < 60
    finally:
        store.close()
