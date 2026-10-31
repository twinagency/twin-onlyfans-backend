"""Concurrent delivery admission on a disposable PostgreSQL database.

Exercises the durable shared admission rules used by native, manual, campaign
and follow-up text: one owner per conversation, bounded dispatch slots, token
bucket, atomic daily accounting, crash-expired recovery and uncertain delivery.
No provider, model or live database is used.
"""
from __future__ import annotations

import os
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.pop("ONLYFANS_PROCESS_ROLE", None)
SERVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE))

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from db.schema import bootstrap
from onlyfans_runtime import concurrent_admission
from twin_bridge import store

ADMIN = os.environ.get("ONLYFANS_TEST_PG_URL")
if not ADMIN:
    sys.exit("FAILED: ONLYFANS_TEST_PG_URL is required; nothing was verified")

NAME = "of_test_concurrent_" + uuid.uuid4().hex[:10]
ADMIN_URL = make_url(ADMIN)
SYNC_URL = ADMIN_URL.set(drivername="postgresql+psycopg2", database=NAME).render_as_string(
    hide_password=False)
TENANT = "tenant-concurrent"
ACCOUNT = "of-concurrent"
CHECKS = 0


def check(value, label=""):
    global CHECKS
    assert value, label
    CHECKS += 1


def policy(session):
    return SimpleNamespace(**dict(session.execute(text(
        "SELECT * FROM of_delivery_policy WHERE account_id=:a FOR UPDATE"
    ), {"a": ACCOUNT}).mappings().one()))


def clear_runtime():
    with engine.begin() as c:
        c.execute(text("DELETE FROM fastt_runtime.engine_conversation_lease"))
        c.execute(text("DELETE FROM fastt_runtime.engine_dispatch_slot"))
        c.execute(text("DELETE FROM fastt_runtime.engine_provider_bucket"))
        c.execute(text("DELETE FROM of_text_intent"))
        c.execute(text(
            "UPDATE of_delivery_policy SET delivery_mode='concurrent',daily_limit_units=NULL,"
            "provider_cooldown_until=NULL,next_dispatch_at=NULL WHERE account_id=:a"
        ), {"a": ACCOUNT})


def add_intent(session, fan, attempt):
    now = datetime.utcnow()
    session.execute(text("SET LOCAL session_replication_role = replica"))
    session.execute(text(
        "INSERT INTO of_text_intent("
        "account_id,intent_id,fan_id,binding_id,command_key,actor,origin,body,command_hash,payload_hash,"
        "control_revision,input_revision,policy_revision,status,created_at,updated_at,expires_at,"
        "attempt_id,dispatched_at) VALUES("
        ":account,:intent,:fan,'binding',:command,'synthetic','automated','synthetic',:hash,:hash,"
        "1,1,1,'dispatching',:now,:now,:expires,:attempt,:now)"
    ), {
        "account": ACCOUNT, "intent": str(uuid.uuid4()), "fan": fan,
        "command": uuid.uuid4().hex, "hash": uuid.uuid4().hex * 2,
        "now": now, "expires": now + timedelta(hours=1), "attempt": str(attempt),
    })


def admit(fan, holder, attempt, *, insert_intent=True):
    with Session(engine) as session:
        with session.begin():
            row = policy(session)
            slot, reason = concurrent_admission.admit(
                session, TENANT, row, account_id=ACCOUNT, fan_id=fan, holder=holder,
                attempt_id=str(attempt), now=datetime.utcnow(),
            )
            if slot is not None and insert_intent:
                add_intent(session, fan, attempt)
            return slot, reason


def release(fan, holder, attempt, outcome):
    with Session(engine) as session:
        with session.begin():
            concurrent_admission.release(
                session, TENANT, ACCOUNT, fan, holder, str(attempt), outcome, datetime.utcnow()
            )


def slots():
    with engine.connect() as c:
        return c.execute(text(
            "SELECT count(*) FROM fastt_runtime.engine_dispatch_slot "
            "WHERE account_id=:a AND state='held'"
        ), {"a": ACCOUNT}).scalar()


def lease(fan):
    with engine.connect() as c:
        return c.execute(text(
            "SELECT state,holder FROM fastt_runtime.engine_conversation_lease "
            "WHERE tenant_id=:t AND account_id=:a AND fan_id=:f"
        ), {"t": TENANT, "a": ACCOUNT, "f": fan}).one()


def race_same_conversation():
    barrier = threading.Barrier(2)
    results = []

    def worker(holder):
        barrier.wait()
        results.append(admit(41, holder, holder + "-attempt", insert_intent=False))

    threads = [threading.Thread(target=worker, args=(f"holder-{i}",)) for i in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return results


def race_daily_cap():
    barrier = threading.Barrier(2)
    results = []

    def worker(fan):
        barrier.wait()
        results.append(admit(fan, f"daily-{fan}", f"daily-attempt-{fan}"))

    with engine.begin() as c:
        c.execute(text(
            "UPDATE of_delivery_policy SET daily_limit_units=1 WHERE account_id=:a"
        ), {"a": ACCOUNT})
    threads = [threading.Thread(target=worker, args=(fan,)) for fan in (51, 52)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return results


def main():
    global engine
    admin = create_engine(
        ADMIN_URL.set(drivername="postgresql+psycopg2").render_as_string(hide_password=False),
        isolation_level="AUTOCOMMIT",
    )
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{NAME}" TEMPLATE template0 ENCODING \'UTF8\''))
    engine = create_engine(SYNC_URL)
    try:
        with engine.begin() as c:
            bootstrap(c)
            c.execute(text("SET LOCAL session_replication_role = replica"))
            c.execute(text(
                "INSERT INTO of_delivery_policy("
                "account_id,enabled,limit_units,used_units,throttle_seconds,delivery_mode,"
                "provider_requests_per_second,provider_burst,provider_max_concurrent,"
                "conversation_min_gap_seconds,daily_limit_units) "
                "VALUES(:a,true,10000,0,20,'concurrent',20.0,10,4,0,NULL)"
            ), {"a": ACCOUNT})
        store.ensure_runtime_schema(SYNC_URL)

        # Two different fans hold separate dispatch slots and leases.
        clear_runtime()
        first = admit(1, "owner-1", "attempt-1")
        second = admit(2, "owner-2", "attempt-2")
        check(first[0] and second[0] and slots() == 2, "different fans are admitted concurrently")
        check(lease(1)[0] == "held" and lease(2)[0] == "held", "both conversations own durable leases")

        # The same conversation has exactly one owner even under a race.
        clear_runtime()
        raced = race_same_conversation()
        check(sorted((slot is not None, reason) for slot, reason in raced)
              == [(False, "conversation_busy"), (True, None)],
              f"one owner wins a same-conversation race: {raced}")

        # A rolling daily cap is atomic across competing conversations.
        clear_runtime()
        capped = race_daily_cap()
        check(sorted((slot is not None, reason) for slot, reason in capped)
              == [(False, "account_daily_cap"), (True, None)],
              f"exactly one send consumes the last daily place: {capped}")

        # Uncertain delivery releases the slot but keeps the conversation closed;
        # another fan continues independently and a new instance sees the state.
        clear_runtime()
        check(admit(10, "owner-10", "attempt-10")[0], "uncertain-delivery fixture admitted")
        release(10, "owner-10", "attempt-10", "unknown")
        check(slots() == 0 and lease(10)[0] == "unresolved", "unknown outcome leaves the conversation unresolved")
        restarted = create_engine(SYNC_URL)
        with Session(restarted) as session:
            with session.begin():
                row = policy(session)
                refused = concurrent_admission.admit(
                    session, TENANT, row, account_id=ACCOUNT, fan_id=10,
                    holder="restart-owner", attempt_id="restart-attempt", now=datetime.utcnow(),
                )
        check(refused == (None, "delivery_unresolved"), "a restarted worker cannot replay an uncertain conversation")
        check(admit(11, "owner-11", "attempt-11")[0], "an unrelated fan proceeds")
        restarted.dispose()

        # A crashed holder stops counting after its lease/slot expires; the
        # conversation can be reacquired by a new worker with a new attempt.
        clear_runtime()
        check(admit(20, "crashed-owner", "crashed-attempt")[0], "crash fixture admitted")
        past = datetime.utcnow() - timedelta(seconds=1)
        with engine.begin() as c:
            c.execute(text(
                "UPDATE fastt_runtime.engine_conversation_lease SET expires_at=:past "
                "WHERE tenant_id=:t AND account_id=:a AND fan_id=20"
            ), {"past": past, "t": TENANT, "a": ACCOUNT})
            c.execute(text(
                "UPDATE fastt_runtime.engine_dispatch_slot SET expires_at=:past "
                "WHERE account_id=:a AND fan_id=20"
            ), {"past": past, "a": ACCOUNT})
        recovered = admit(20, "recovered-owner", "recovered-attempt")
        check(recovered[0] and slots() == 1, "expired crash ownership is reclaimed without a duplicate slot")

        # Switching one account back to gap while a concurrent send is in flight
        # makes new concurrent admissions use the legacy path; the in-flight
        # attempt can still settle through the durable owner.
        clear_runtime()
        check(admit(30, "owner-30", "attempt-30")[0], "mode-switch fixture admitted")
        with engine.begin() as c:
            c.execute(text(
                "UPDATE of_delivery_policy SET delivery_mode='gap' WHERE account_id=:a"
            ), {"a": ACCOUNT})
        with Session(engine) as session:
            with session.begin():
                row = policy(session)
                switched = concurrent_admission.admit(
                    session, TENANT, row, account_id=ACCOUNT, fan_id=31,
                    holder="gap-owner", attempt_id="gap-attempt", now=datetime.utcnow(),
                )
        check(switched == (None, None), "gap mode hands new work to the legacy path")
        release(30, "owner-30", "attempt-30", "accepted")
        check(slots() == 0 and lease(30)[0] == "released", "the in-flight concurrent attempt settles after the switch")

        print(f"concurrent delivery postgres: all cases passed ({CHECKS} checks)")
    finally:
        engine.dispose()
        store.dispose_pools()
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{NAME}" WITH (FORCE)'))
        admin.dispose()


if __name__ == "__main__":
    main()
