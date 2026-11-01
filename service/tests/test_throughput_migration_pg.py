"""Migrations 0102_daily_send_cap and 0103_concurrent_delivery on a THROWAWAY PostgreSQL
(ONLYFANS_TEST_PG_URL = admin URL of a disposable server; the script creates and drops its own database). Plain script.

Proves: a fresh bootstrap reaches 0103 and passes check_schema; the daily and provider
admission settings have their defaults and bounds; existing rows remain in legacy ``gap``
mode; the lifetime limit_units 0..10000, queued_count 0..1000 and throttle_seconds
1..86400 checks are untouched; downgrade/upgrade round-trips."""
import os, sys, uuid
from pathlib import Path
os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from alembic import command

ADMIN = os.environ.get("ONLYFANS_TEST_PG_URL")
if not ADMIN:
    sys.exit("FAILED: ONLYFANS_TEST_PG_URL is required; nothing was verified")
from db.schema import HEAD, _config, bootstrap, check_schema

CHECKS = 0
def check(v, label=""):
    global CHECKS
    assert v, label
    CHECKS += 1

name = "of_test_thr_" + uuid.uuid4().hex[:10]
admin = create_engine(ADMIN, isolation_level="AUTOCOMMIT")
with admin.connect() as c:
    c.exec_driver_sql(f'CREATE DATABASE "{name}" TEMPLATE template0 ENCODING \'UTF8\'')
engine = create_engine(admin.url.set(database=name).render_as_string(hide_password=False), connect_args={"options": "-c timezone=UTC"})
try:
    with engine.begin() as c:
        bootstrap(c)
    check(HEAD == "0107_active_selling_quote")
    with engine.begin() as c:
        check_schema(c)
        c.execute(text("SET session_replication_role = replica"))        # throwaway DB: skip the FK chain to accounts/runtime
        c.execute(text("INSERT INTO of_delivery_policy(account_id,limit_units) VALUES('a-null',500)"))
        row = c.execute(text(
            "SELECT daily_limit_units,delivery_mode,provider_requests_per_second,provider_burst,"
            "provider_max_concurrent,conversation_min_gap_seconds,provider_cooldown_until "
            "FROM of_delivery_policy WHERE account_id='a-null'")).one()
        check(row[0] is None, "daily cap defaults to off")
        check(row[1] == "gap", "existing/new policy rows default to the legacy gap mode")
        check(row[2:] == (1.0, 5, 4, 0, None), f"provider admission defaults are conservative ({row[2:]})")
        for value in (0, 100, 20000):
            c.execute(text("INSERT INTO of_delivery_policy(account_id,limit_units,daily_limit_units) VALUES(:a,500,:v)"), {"a": f"a-{value}", "v": value})
    def rejected(sql, **params):
        try:
            with engine.begin() as c:
                c.execute(text("SET session_replication_role = replica"))
                c.execute(text(sql), params)
        except IntegrityError:
            return True
        return False
    check(rejected("INSERT INTO of_delivery_policy(account_id,limit_units,daily_limit_units) VALUES('x1',500,20001)"), "daily over 20000")
    check(rejected("INSERT INTO of_delivery_policy(account_id,limit_units,daily_limit_units) VALUES('x2',500,-1)"), "daily negative")
    check(rejected("INSERT INTO of_delivery_policy(account_id,limit_units) VALUES('x3',10001)"), "lifetime limit still capped at 10000")
    check(rejected("INSERT INTO of_delivery_policy(account_id,queued_count) VALUES('x4',1001)"), "queued_count still capped at 1000")
    check(rejected("INSERT INTO of_delivery_policy(account_id,throttle_seconds) VALUES('x5',0)"), "throttle_seconds 0 still refused")
    check(not rejected("INSERT INTO of_delivery_policy(account_id,limit_units,throttle_seconds) VALUES('x6',10000,86400)"), "old bounds still accepted")
    check(rejected("INSERT INTO of_delivery_policy(account_id,delivery_mode) VALUES('x7','fast')"), "unknown delivery mode refused")
    check(rejected("INSERT INTO of_delivery_policy(account_id,provider_requests_per_second) VALUES('x8',0)"), "zero provider rate refused")
    check(rejected("INSERT INTO of_delivery_policy(account_id,provider_burst) VALUES('x9',0)"), "zero provider burst refused")
    check(rejected("INSERT INTO of_delivery_policy(account_id,provider_max_concurrent) VALUES('x10',65)"), "provider concurrency over 64 refused")
    check(rejected("INSERT INTO of_delivery_policy(account_id,conversation_min_gap_seconds) VALUES('x11',3601)"), "per-fan gap over an hour refused")
    check(not rejected(
        "INSERT INTO of_delivery_policy(account_id,delivery_mode,provider_requests_per_second,provider_burst,"
        "provider_max_concurrent,conversation_min_gap_seconds) VALUES('x12','concurrent',4.5,8,8,0)"),
        "supported concurrent controls accepted")
    with engine.begin() as c:
        command.downgrade(_config(c), "0100_manual_free_media")
        cols = {r[0] for r in c.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name='of_delivery_policy'"))}
        check("daily_limit_units" not in cols, "downgrade drops the column")
        check("delivery_mode" not in cols and "provider_burst" not in cols,
              "downgrade removes the concurrent policy columns")
        check(c.execute(text("SELECT count(*) FROM of_delivery_policy")).scalar() >= 4, "downgrade keeps the rows")
    with engine.begin() as c:
        command.upgrade(_config(c), HEAD)
        check_schema(c)
        check(c.execute(text("SELECT daily_limit_units FROM of_delivery_policy WHERE account_id='a-null'")).scalar() is None, "re-upgrade: NULL")
        check(c.execute(text("SELECT delivery_mode FROM of_delivery_policy WHERE account_id='a-null'")).scalar() == "gap",
              "re-upgrade restores the legacy default")
finally:
    engine.dispose()
    with admin.connect() as c:
        c.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
    admin.dispose()
print(f"throughput migration postgres: all cases passed ({CHECKS} checks)")
