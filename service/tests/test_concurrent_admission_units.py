"""Unit checks for the shared per-conversation/account admission rules and the send gate."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from native_pilot.send_gate import SendGate
from native_pilot import admission
from onlyfans_runtime import concurrent_admission


def policy(**kw):
    base = dict(delivery_mode="concurrent", provider_requests_per_second=10.0, provider_burst=10,
                provider_max_concurrent=2, conversation_min_gap_seconds=0,
                provider_cooldown_until=None, next_dispatch_at=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_concurrent_migration_is_the_release_head():
    from db import schema
    assert schema.HEAD == "0102_concurrent_delivery"
    assert schema.REVISION_RANK[schema.CONCURRENT_DELIVERY] == 102


def session():
    engine = create_engine("sqlite:///:memory:")
    return engine, Session(engine)


def test_conversation_and_account_bounds():
    engine, s = session()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    p = policy()
    with s.begin():
        slot1, reason = concurrent_admission.admit(s, "t", p, account_id="a", fan_id=1, holder="owner-1",
                                                   attempt_id="att-1", now=now)
        assert slot1 and reason is None
        assert concurrent_admission.admit(s, "t", p, account_id="a", fan_id=1, holder="owner-2",
                                          attempt_id="att-2", now=now) == (None, "conversation_busy")
        slot2, reason = concurrent_admission.admit(s, "t", p, account_id="a", fan_id=2, holder="owner-2",
                                                   attempt_id="att-2", now=now)
        assert slot2 and reason is None
        assert concurrent_admission.admit(s, "t", p, account_id="a", fan_id=3, holder="owner-3",
                                          attempt_id="att-3", now=now) == (None, "provider_concurrency_limit")
        concurrent_admission.release(s, "t", "a", 1, "owner-1", "att-1", "accepted", now)
        slot3, reason = concurrent_admission.admit(s, "t", p, account_id="a", fan_id=3, holder="owner-3",
                                                   attempt_id="att-3", now=now)
        assert slot3 and reason is None
    engine.dispose()


def test_token_bucket_and_takeover_hold():
    engine, s = session()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    p = policy(provider_burst=1, provider_requests_per_second=0.1)
    with s.begin():
        assert concurrent_admission.admit(s, "t", p, account_id="a", fan_id=1, holder="o1",
                                          attempt_id="1", now=now)[1] is None
        assert concurrent_admission.admit(s, "t", p, account_id="a", fan_id=2, holder="o2",
                                          attempt_id="2", now=now) == (None, "account_throttled")
        concurrent_admission.release(s, "t", "a", 1, "o1", "1", "unknown", now)
        assert concurrent_admission.admit(s, "t", p, account_id="a", fan_id=2, holder="o2",
                                          attempt_id="2", now=now + timedelta(seconds=20))[1] is None
        # o1's conversation is unresolved: a replacement owner cannot advance it.
        assert concurrent_admission.admit(s, "t", p, account_id="a", fan_id=1, holder="o3",
                                          attempt_id="3", now=now + timedelta(seconds=21)) == (None, "delivery_unresolved")
    engine.dispose()


def test_transient_refusal_does_not_leave_a_conversation_lease():
    engine, s = session()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    p = policy(provider_burst=1, provider_requests_per_second=0.01, provider_max_concurrent=1)
    with s.begin():
        assert concurrent_admission.admit(
            s, "t", p, account_id="a", fan_id=1, holder="o1",
            attempt_id="att-1", now=now)[1] is None
        # Account concurrency is full: the refusal must not reserve fan 2.
        assert concurrent_admission.admit(
            s, "t", p, account_id="a", fan_id=2, holder="o2",
            attempt_id="att-2", now=now) == (None, "provider_concurrency_limit")
        held = s.execute(text(
            "SELECT fan_id FROM engine_conversation_lease WHERE account_id='a'"
        )).scalars().all()
        assert held == [1]
        concurrent_admission.release(s, "t", "a", 1, "o1", "att-1", "accepted", now)
        # The bucket is empty as well: this refusal must not reserve fan 2 either.
        assert concurrent_admission.admit(
            s, "t", p, account_id="a", fan_id=2, holder="o2",
            attempt_id="att-2", now=now) == (None, "account_throttled")
        held = s.execute(text(
            "SELECT fan_id FROM engine_conversation_lease WHERE account_id='a'"
        )).scalars().all()
        assert held == [1]
    engine.dispose()


def test_cooldown_uses_the_later_transition_stamp():
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    row = policy(provider_cooldown_until=now - timedelta(seconds=1),
                 next_dispatch_at=now + timedelta(seconds=10))
    assert admission.cooldown_at(row) == row.next_dispatch_at


def test_send_gate_bounded_concurrency_and_order():
    async def run():
        gate = SendGate(max_concurrency=1)
        order = []
        await gate.acquire(lambda: None, 5, priority=99, key="base")

        async def worker(i):
            await gate.acquire(lambda: None, 5, priority=10 - i, key=i)
            order.append(i)
            await asyncio.sleep(0.01)
            gate.release(i)

        tasks = [asyncio.create_task(worker(i)) for i in range(4)]
        await asyncio.sleep(0)
        gate.release("base")
        await asyncio.gather(*tasks)
        assert gate.in_flight == 0 and gate.max_waiting >= 1
        # The held slot is handed to the highest priority waiter first.
        assert order[0] == 3

    asyncio.run(run())
