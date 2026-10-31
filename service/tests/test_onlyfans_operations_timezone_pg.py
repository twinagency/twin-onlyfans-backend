"""The actual operations projection uses UTC regardless of PostgreSQL session zone."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from db.models import AccountAiConfig, Fan, GrokDailyCost, OfConversationState
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


def test_operations_blocked_work_counts_only_for_fans_eligible_for_automation(database, monkeypatch):
    """A blocked row on a paused/taken-over/muted/held fan is stale bookkeeping, not backlog."""
    from onlyfans_read import repo

    url, engine = database
    store = seed(url, engine)  # fan 101: automated, no takeover
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    others = {  # fan_id -> state overrides (all of these must NOT count)
        102: {"mode": "paused"},
        103: {"mode": "human_controlled"},
        104: {"takeover_state": "requested"},
        105: {"unresolved_attempt_id": "att-1"},
        106: {"mode": "no_contact"},
        107: {},  # muted below
    }
    with Session(engine) as s, s.begin():
        for fan_id, over in others.items():
            s.add(Fan(account_id="acc-a", fan_id=fan_id, is_muted=fan_id == 107))
        s.flush()
        for fan_id, over in others.items():
            s.add(OfConversationState(**{"account_id": "acc-a", "fan_id": fan_id, "mode": "automated",
                "revision": 0, "takeover_state": "none", "requires_attention": False, **over}))
        s.flush()
        for fan_id in (101, *others):
            s.add(OfWork(account_id="acc-a", fan_id=fan_id, generation=1, status="blocked",
                first_input_at=now - timedelta(days=5), last_input_at=now - timedelta(days=5),
                due_at=now - timedelta(days=5), target_message_id=fan_id, control_revision=0))

    async def project():
        async_engine = create_async_engine(make_url(url).set(drivername="postgresql+asyncpg"))

        @asynccontextmanager
        async def session():
            async with async_sessionmaker(async_engine)() as s:
                yield s

        monkeypatch.setattr(repo, "get_session", session)
        try:
            return await repo.SqlOnlyFansReadRepo().operations(tenant_id="tenant-a", account_id="acc-a")
        finally:
            await async_engine.dispose()

    try:
        status = asyncio.run(project())
        assert status["work"]["blocked"] == 1, "only the eligible fan's blocked row is backlog"
        assert status["work"]["waiting"] == 0 and status["work"]["claimed"] == 0
        assert status["work"]["oldestWaitingAgeSeconds"] is None
    finally:
        store.close()
