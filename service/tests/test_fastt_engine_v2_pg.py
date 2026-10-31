"""Engine v2 on REAL PostgreSQL: scoped write locks, the new tables (model == upgrade SQL, rollback SQL), runner lease
and work board. ONLYFANS_TEST_PG_URL must be an admin URL of a disposable server (this script creates and drops its
own database). Run as a plain script."""
from __future__ import annotations

import os
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.pop("ONLYFANS_PROCESS_ROLE", None)
SERVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE))

from sqlalchemy import create_engine, inspect, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

ADMIN = os.environ.get("ONLYFANS_TEST_PG_URL")
if not ADMIN:
    sys.exit("FAILED: ONLYFANS_TEST_PG_URL is required; nothing was verified")
NAME = "of_test_v2_" + uuid.uuid4().hex[:10]
admin_url = make_url(ADMIN)
SYNC = admin_url.set(drivername="postgresql+psycopg2", database=NAME).render_as_string(hide_password=False)
os.environ["DATABASE_URL"] = admin_url.set(drivername="postgresql+asyncpg", database=NAME).render_as_string(hide_password=False)

from twin_bridge import store  # noqa: E402
from native_pilot.engine_state import RunnerLease, WorkBoard  # noqa: E402
from native_pilot.ledger import Ledger, Refused  # noqa: E402

CHECKS = 0


def check(v, label=""):
    global CHECKS
    assert v, label
    CHECKS += 1


def hold_lock(scope, seconds, log):
    t0 = time.monotonic()
    with store.transaction(SYNC, scope):
        log.append((scope, "in", time.monotonic() - t0))
        time.sleep(seconds)


def timed(*scopes, seconds=0.6):
    log = []
    threads = [threading.Thread(target=hold_lock, args=(s, seconds, log)) for s in scopes]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return time.monotonic() - t0


def main():
    admin = create_engine(admin_url.set(drivername="postgresql+psycopg2").render_as_string(hide_password=False),
                          isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{NAME}"'))
    try:
        store.ensure_runtime_schema(SYNC)
        # 1. locks: other accounts run in parallel, the same account and any unscoped writer are serialised
        check(timed("a", "b", "c") < 1.0, "three accounts' writers run in parallel")
        check(timed("a", "a") >= 1.1, "one account's writers are serialised")
        check(timed("a", None) >= 1.1, "an unscoped writer excludes scoped ones")
        check(timed(None, None) >= 1.1, "unscoped writers exclude each other (the previous behaviour)")
        # 2. the new tables: the engine's model equals the reviewable upgrade SQL; the rollback removes only them
        eng = create_engine(SYNC)
        insp = inspect(eng)
        have = {t: {c["name"]: str(c["type"]) for c in insp.get_columns(t, schema="fastt_runtime")}
                for t in ("engine_runner_lease", "engine_work")}
        check(set(have["engine_runner_lease"]) == {"account_id", "owner_token", "holder", "generation", "acquired_at",
                                                  "heartbeat_at", "expires_at"})
        check(set(have["engine_work"]) == {"account_id", "fan_id", "state", "reason", "message_id", "waiting_since",
                                           "state_since", "attempts", "updated_at"})
        sql_dir = SERVICE / "native_pilot" / "sql"
        with eng.begin() as c:
            c.execute(text("DROP TABLE fastt_runtime.engine_work; DROP TABLE fastt_runtime.engine_runner_lease"))
            c.exec_driver_sql((sql_dir / "engine_v2_upgrade.sql").read_text())
            c.exec_driver_sql((sql_dir / "engine_v2_upgrade.sql").read_text())      # idempotent
        insp = inspect(eng)
        again = {t: {c["name"]: str(c["type"]) for c in insp.get_columns(t, schema="fastt_runtime")}
                 for t in ("engine_runner_lease", "engine_work")}
        check(again == have, "upgrade SQL builds exactly what the model builds")
        before = set(insp.get_table_names(schema="fastt_runtime"))
        with eng.begin() as c:
            c.exec_driver_sql((sql_dir / "engine_v2_rollback.sql").read_text())
        after = set(inspect(eng).get_table_names(schema="fastt_runtime"))
        check(before - after == {"engine_work", "engine_runner_lease"}, "rollback removes only the two new tables")
        store.dispose_pools()
        store.ensure_runtime_schema(SYNC)       # the engine recreates them on start
        # 3. runner lease and work board on PostgreSQL
        led = Ledger(SYNC, allowance=True, lock_scope="acct")
        now = datetime(2026, 10, 30, 12)
        a, b = RunnerLease(led, "acct", "a:1", 60), RunnerLease(led, "acct", "b:2", 60)
        check(a.acquire(now) == 1)
        try:
            b.acquire(now + timedelta(seconds=5))
        except Refused as exc:
            check(exc.reason == "runner-lease-held")
        else:
            raise AssertionError("second holder")
        check(b.acquire(now + timedelta(seconds=70)) == 2 and a.renew(now + timedelta(seconds=71)) is False)
        # two processes racing for a free lease: exactly one wins
        wins = []

        def race(name):
            try:
                RunnerLease(Ledger(SYNC, allowance=True, lock_scope="racers"), "racers", name, 60).acquire(now)
                wins.append(name)
            except Refused:
                pass
        ts = [threading.Thread(target=race, args=(f"p{i}",)) for i in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        check(len(wins) == 1, f"exactly one of six racing processes owns the account ({wins})")
        board = WorkBoard(led, "acct")
        board.set(5, "blocked", "delivery-unresolved-awaiting-proof", message_id=9, waiting_since=now)
        board.set(6, "quiet", "burst-quiet-window")
        check(board.flush(now) == 2)
        with eng.connect() as c:
            rows = c.execute(text("SELECT fan_id,state,reason FROM fastt_runtime.engine_work ORDER BY fan_id")).all()
        check(rows == [(5, "blocked", "delivery-unresolved-awaiting-proof"), (6, "quiet", "burst-quiet-window")])
        eng.dispose()
        store.dispose_pools()
    finally:
        store.dispose_pools()
        with admin.connect() as c:
            c.execute(text(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{NAME}'"))
            c.execute(text(f'DROP DATABASE IF EXISTS "{NAME}"'))
    print(f"fastt engine v2 postgres: all cases passed ({CHECKS} checks)")


main()
