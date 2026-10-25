"""Real PostgreSQL race tests for the pilot's first daily budget reservations."""
import asyncio
from datetime import datetime
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from db.models import Account, AccountAiConfig, Fan, GrokCall, GrokDailyCost
from native_pilot.budget import DailyBudget, Reservation
from native_pilot.scope import AccountScope


@pytest.mark.parametrize("limited", [False, True])
def test_concurrent_first_reservations_do_not_race_or_overspend(database, monkeypatch, limited):
    import importlib
    db_engine = importlib.import_module("db.engine")
    url, engine = database
    account = "of-900000001"
    budget = DailyBudget(AccountScope.parse(account), "openrouter/deepseek-v4.1-flash",
                         endpoint="https://openrouter.ai/api/v1/chat/completions")
    reserve = budget.estimate(8000, 4096)
    cap = (reserve + 99) // 100 if limited else 1000000
    with Session(engine) as session, session.begin():
        session.add(Account(id=account)); session.flush()
        session.add(AccountAiConfig(account_id=account, daily_cost_cap_cents=cap))
        session.add_all([Fan(account_id=account, fan_id=1000000+i) for i in range(32)])

    async def exercise():
        from sqlalchemy.engine import make_url
        async_engine = create_async_engine(make_url(url).set(drivername="postgresql+asyncpg"),
                                           pool_size=8, max_overflow=0)
        monkeypatch.setattr(db_engine, "async_session_factory",
                            async_sessionmaker(async_engine, expire_on_commit=False))
        try:
            return await asyncio.wait_for(asyncio.gather(*(
                budget.reserve(fan_id=1000000+i, request_bytes=8000, max_output_tokens=4096)
                for i in range(32)), return_exceptions=True), timeout=20)
        finally:
            await async_engine.dispose()

    outcomes = asyncio.run(exercise())
    accepted = [r for r in outcomes if isinstance(r, Reservation)]
    failures = [r for r in outcomes if isinstance(r, BaseException)]
    if limited:
        assert len(accepted) == min(32, cap * 100 // reserve)
        assert all(type(error).__name__ == "LLMCapExceeded" for error in failures)
    else:
        assert len(accepted) == 32 and not failures
    with Session(engine) as session:
        rows = list(session.scalars(select(GrokDailyCost).where(GrokDailyCost.account_id == account)))
        calls = list(session.scalars(select(GrokCall).where(GrokCall.account_id == account)))
        assert len(rows) == 1
        assert rows[0].call_count == len(accepted) == len(calls)
        assert rows[0].cost_cents == len(accepted) * reserve <= cap * 100
        assert all(c.status == "pending" for c in calls)
