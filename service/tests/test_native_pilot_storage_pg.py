"""Native pilot storage on REAL PostgreSQL: same rules as the SQLite suites.

Synthetic data only; a missing server is a failure, never a skip.
ONLYFANS_TEST_PG_URL must be an admin URL of a disposable server on which this
script may create and drop its own uniquely named database (prefix from
ONLYFANS_TEST_DB_PREFIX, default ``of_test_``). Run as a plain script.

It proves, against the real schema produced by ``bootstrap`` (application revision 0091,
which this change deliberately does NOT advance):

1. the schema check passes AT 0091 both before and after the nine bridge/pilot tables are
   created idempotently in the separate ``fastt_runtime`` schema (none in ``public``), with the
   boolean default fixed for PostgreSQL; the ORIGINAL release (8a427a3) ``check_schema`` still
   passes on that database (rollback compatibility, run in a subprocess from a clean copy);
2. the ENTIRE existing ledger suite (admission before each provider POST, unknown
   outcome stops and is never resent, send allowance, model-call reservation,
   receipts surviving stop/deadline, races) passes unchanged on PostgreSQL;
3. ownership reservation, the durable writer marker handshake and the
   transport boundary behave as they do on SQLite;
4. the ENTIRE existing assembled-runner suite (real ledger + guarded_send +
   OFClient with fake HTTP) passes unchanged on PostgreSQL.

The only test-side adaptation is the fixture: it creates rows through the real
schema (typed columns) instead of the SQLite scratch tables.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.pop("ONLYFANS_PROCESS_ROLE", None)
SERVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import bindparam, create_engine, inspect, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

ADMIN_URL = os.environ.get("ONLYFANS_TEST_PG_URL")
if not ADMIN_URL:
    sys.exit("FAILED: ONLYFANS_TEST_PG_URL (admin URL of a disposable PostgreSQL server) is required; nothing was verified")
NAME = os.environ.get("ONLYFANS_TEST_DB_PREFIX", "of_test_") + "pilot_" + uuid.uuid4().hex[:12]
_admin_url = make_url(ADMIN_URL)
SYNC_URL = _admin_url.set(drivername="postgresql+psycopg2", database=NAME).render_as_string(hide_password=False)
ASYNC_URL = _admin_url.set(drivername="postgresql+asyncpg", database=NAME).render_as_string(hide_password=False)
os.environ["DATABASE_URL"] = ASYNC_URL  # before any application import
os.environ["TWIN_BRIDGE_ENABLED"] = "0"
os.environ["TWIN_BRIDGE_SEND_ENABLED"] = "0"

import test_native_pilot_ledger as ledger_test  # noqa: E402
import test_native_pilot_runner as runner_test  # noqa: E402
from native_pilot.ledger import Ledger, Refused  # noqa: E402
from twin_bridge import store  # noqa: E402

CHECKS = 0


def check(value, label=""):
    global CHECKS
    assert value, label
    CHECKS += 1


TABLES = ("twin_bridge_inputs", "twin_bridge_turns", "twin_bridge_turn_members", "twin_bridge_conversations",
          "twin_bridge_send_attempts", "twin_bridge_writes", "native_pilots", "native_pilot_inputs",
          "native_pilot_receipts")


def rows(sql, args=()):
    with store.transaction(SYNC_URL) as con:
        return con.execute(sql, args).fetchall()


def reset():
    # DELETE (children first via ON DELETE CASCADE from accounts) is far cheaper than TRUNCATE ... CASCADE
    # over the whole schema, and the fixtures expect attempt/run ids to restart at 1.
    for table in (*TABLES, "messages", "automation_runs", "accounts"):
        rows("DELETE FROM " + table)
    for sequence in ("twin_bridge_send_attempts_id_seq", "automation_runs_id_seq"):
        rows("ALTER SEQUENCE " + sequence + " RESTART WITH 1")


def seed_conversation(account, fan, *, state="active", revision=4, limit=3, used=3):
    L = ledger_test
    rows("INSERT INTO twin_bridge_conversations(account_id,fan_id,creator_slug,conversation_id,scope_fingerprint,state,"
         "revision,reserved,send_budget_limit,send_budget_used,updated_at) VALUES(?,?,'synthetic','c','f',?,?,?,?,?,?)",
         (account, fan, state, revision, True, limit, used, L.NOW.isoformat()))


class PgFixture(ledger_test.Fixture):
    """The SQLite fixture's interface over the real PostgreSQL schema."""

    def __init__(self, root, name):
        L = ledger_test
        reset()
        seed_conversation(L.ACCT, L.FAN)
        rows("INSERT INTO twin_bridge_send_attempts(account_id,fan_id,turn_id,attempt_no,state,owner_token,revision) "
             "VALUES(?,?,'previous-twin-turn',1,'sent','old-owner',3)", (L.ACCT, L.FAN))
        rows("INSERT INTO twin_bridge_turns(account_id,fan_id,turn_id,creator_slug,conversation_id,scope_fingerprint,"
             "member_ids_json,text,event_json,occurred_at,received_at,state,revision,generation_attempts,status_polls,"
             "ack_attempts,cancel_attempts) VALUES(?,?,'previous-turn','s','c','f','[]','t','{}',?,?,'delivered',1,0,0,0,0)",
             (L.ACCT, L.FAN, L.NOW.isoformat(), L.NOW.isoformat()))
        self.path = SYNC_URL
        self.ledger = Ledger(self.path)
        self.args = dict(pilot_id="synthetic-test", account_id=L.ACCT, fan_id=L.FAN, owner_token=L.OWNER,
                         cutoff=L.NOW, deadline=L.NOW + timedelta(minutes=30), limit=10, now=L.NOW)
        self.call = dict(pilot_id="synthetic-test", owner_token=L.OWNER, now=L.NOW + timedelta(seconds=10))
        self.pilot = None

    _BOOL = re.compile(r"\b(is_unsent|is_tip|reserved|enabled)=([01])\b")

    def sql(self, sql, args=()):
        head = sql.lstrip().upper()
        if head.startswith(("CREATE TABLE", "ALTER TABLE")):
            return []  # the real schema already has every table and column
        sql = self._BOOL.sub(lambda m: f"{m.group(1)}={'true' if m.group(2) == '1' else 'false'}", sql)
        if sql.startswith("INSERT INTO twin_bridge_writes VALUES("):
            sql = sql.replace("INSERT INTO twin_bridge_writes VALUES(",
                              "INSERT INTO twin_bridge_writes(account_id,fan_id,holder,state,expires_at) VALUES(", 1)
        elif sql.startswith("INSERT INTO accounts VALUES(?)"):
            sql = "INSERT INTO accounts(id,is_active_default) VALUES(?,false)"
        elif sql.startswith("INSERT INTO scheduled_jobs VALUES(?,?,?)"):
            sql = ("INSERT INTO scheduled_jobs(account_id,status,kind,payload_json,run_at,attempts) "
                   "VALUES(?,?,?,'{}',CURRENT_TIMESTAMP,0)")
            # The real column is NOT NULL: a job with no kind is the empty kind (still not 'gen_info').
            args = (args[0], args[1], "" if args[2] is None else args[2])
        elif sql.startswith("INSERT INTO content_offers VALUES("):
            # Runner fixture shape: (account, fan, status, mode, item_id); the real table has more NOT NULL columns.
            sql = ("INSERT INTO content_offers(account_id,fan_id,status,mode,item_id,price_cents,tip_unlock_cents,"
                   "tips_accum_cents,offered_at,updated_at) "
                   + sql[len("INSERT INTO content_offers "):].rstrip(")")
                   + ",300,0,0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)")
        elif sql.startswith("INSERT INTO catalog_items VALUES("):
            # Runner fixture shape: (id, account, kind, media_ids, preview_media_ids, price, is_free_teaser, enabled, pricing_mode).
            m = re.fullmatch(r"INSERT INTO catalog_items VALUES\((\d+),\?,'(\w+)','(\[[\d, ]+\])','(\[[\d, ]*\])',(\d+),([01]),([01]),'(\w+)'\)", sql)
            sql = ("INSERT INTO catalog_items(id,account_id,kind,media_ids,preview_media_ids,price_cents,tip_unlock_cents,"
                   f"is_free_teaser,tags,enabled,pricing_mode) VALUES({m.group(1)},?,'{m.group(2)}','{m.group(3)}','{m.group(4)}',{m.group(5)},0,"
                   f"{'true' if m.group(6) == '1' else 'false'},'[]',{'true' if m.group(7) == '1' else 'false'},'{m.group(8)}')")
        return rows(sql, args)

    def source(self, mid=100, **overrides):
        L = ledger_test
        values = dict(account_id=L.ACCT, fan_id=L.FAN, message_id=mid, direction="in", sender_name="synthetic",
                      body="neutral synthetic test", media_ids="[]", media_count=0, price_cents=0, is_tip=False,
                      is_unsent=False, is_promise=False,
                      created_at=(L.NOW + timedelta(seconds=1)).isoformat(sep=" "),
                      ingested_at=(L.NOW + timedelta(seconds=2)).isoformat(sep=" "), raw_json='{"synthetic":true}')
        values.update(overrides)
        for key in ("is_tip", "is_unsent"):
            values[key] = bool(values[key])
        try:
            self.sql("INSERT INTO messages(" + ",".join(values) + ") VALUES(" + ",".join("?" for _ in values) + ")",
                     tuple(values.values()))
        except store.psycopg2.errors.DataError:
            # SQLite accepts the text "bad" as a timestamp; a typed column cannot
            # even store it, so that malformed canonical row can never exist here.
            check(values["created_at"] == "bad")


RELEASE_SERVICE = os.environ.get(
    "ONLYFANS_RELEASE_SERVICE_DIR",
    "/tmp/claude-1000/-home-ronan-twin/1f28717a-be90-4d45-ac41-136eed78da59/scratchpad/rel/service")
LIVE_REVISION = "0091_launch_controls"


def original_release_check():
    """Run the ORIGINAL release's check_schema (clean 8a427a3 copy) against this database."""
    import subprocess
    if not (Path(RELEASE_SERVICE) / "db" / "schema.py").is_file():
        sys.exit("FAILED: ONLYFANS_RELEASE_SERVICE_DIR (clean 8a427a3 service dir) is required for the rollback check")
    code = ("import sys; from sqlalchemy import create_engine; from db.schema import HEAD, check_schema; "
            "e=create_engine(sys.argv[1]); c=e.connect(); check_schema(c); print('release-check-ok', HEAD)")
    env = {"PATH": os.environ.get("PATH", ""), "PYTHON_DOTENV_DISABLED": "1", "DATABASE_URL": ASYNC_URL}
    done = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv.pop(1)); exec(sys.argv.pop(1))",
                           RELEASE_SERVICE, code, SYNC_URL],
                          cwd=RELEASE_SERVICE, env=env, capture_output=True, text=True, timeout=120)
    check(done.returncode == 0 and f"release-check-ok {LIVE_REVISION}" in done.stdout, done.stdout + done.stderr)


def schema_checks():
    from db.schema import HEAD, _metadata, check_schema
    check(HEAD == LIVE_REVISION, "application schema head stays at 0091")
    check(not set(TABLES) & set(_metadata().tables), "app metadata/check_schema must not include the pilot tables")
    with create_engine(SYNC_URL).connect() as conn:
        check_schema(conn)  # fresh 0091 database, pilot tables not created yet
        check(set(inspect(conn).get_schema_names()) >= {"public"} and "fastt_runtime" not in inspect(conn).get_schema_names())
    original_release_check()
    # Fail-open: with the fastt_runtime schema absent the OFClient writer-marker path (guarded_send) still
    # lets an ordinary, bridge-disabled send through exactly as on a database that never had the table.
    from twin_bridge import coordination as co
    with co.guarded_send("1", 2, purpose="other"):
        check(True)
    check(co.in_flight_count("1", 2) == 0)
    # Idempotent creation by the runner/control/bridge entry points.
    store.ensure_runtime_schema(SYNC_URL)
    store.dispose_pools()  # force a second creation attempt
    store.ensure_runtime_schema(SYNC_URL)
    with create_engine(SYNC_URL).connect() as conn:
        check_schema(conn)
        check(conn.scalar(text("SELECT version_num FROM alembic_version")) == LIVE_REVISION)
        check(set(inspect(conn).get_table_names(schema="fastt_runtime")) == set(TABLES))
        check(not set(TABLES) & set(inspect(conn).get_table_names()), "no pilot table in public")
        check(conn.scalar(text("SELECT count(*) FROM information_schema.tables WHERE table_name IN :t "
                               "AND table_schema<>'fastt_runtime'").bindparams(bindparam("t", expanding=True)),
                          {"t": list(TABLES)}) == 0)
    original_release_check()  # rollback compatibility: the previous image's own schema check still passes
    # The default is PostgreSQL-valid: a conversation inserted without `reserved` is reserved.
    reset()
    rows("INSERT INTO twin_bridge_conversations(account_id,fan_id,creator_slug,conversation_id,scope_fingerprint,state,"
         "revision,send_budget_limit,send_budget_used) VALUES('1',2,'s','c','f','active',1,0,0)")
    check(rows("SELECT reserved FROM twin_bridge_conversations")[0][0] is True)


async def ownership():
    from twin_bridge import coordination as co
    from db.engine import get_session
    L = ledger_test
    A, F = L.ACCT, L.FAN
    reset()
    check(co.store_target() == SYNC_URL)
    check(co.sqlite_path() is None)

    # Conversation rows are created by the dialect-aware insert, never overwritten.
    now = datetime.utcnow()
    async with get_session() as session:
        row = await co.ensure_conversation(session, account_id=A, fan_id=F, creator_slug="s", conversation_id="c",
                                           scope_fingerprint="f", now=now, send_budget_limit=3)
        check(row.state == "active" and row.reserved is True and row.send_budget_limit == 3)
    async with get_session() as session:
        row = await co.ensure_conversation(session, account_id=A, fan_id=F, creator_slug="other", conversation_id="x",
                                           scope_fingerprint="y", now=now, send_budget_limit=99)
        check(row.creator_slug == "s" and row.send_budget_limit == 3)

    # Reservation: one live owner, a different live owner is refused, the same owner renews.
    check(await co.reserve_ownership(account_id=A, fan_id=F, owner_token="owner-a-000000000001", now=now, ttl_s=60))
    check(not await co.reserve_ownership(account_id=A, fan_id=F, owner_token="owner-b-000000000001", now=now, ttl_s=60))
    co.clear_owner(A, F, "owner-b-000000000001")
    check(rows("SELECT owner_token,reserved FROM twin_bridge_conversations")[0] == ("owner-a-000000000001", True))
    check(await co.reserve_ownership(account_id=A, fan_id=F, owner_token="owner-a-000000000001", now=now, ttl_s=60))
    check(rows("SELECT revision FROM twin_bridge_conversations")[0][0] == 1, "a plain renewal never advances the fence")

    # The durable reservation (not the process mirror) refuses every other writer, even after a restart.
    co.drop_reservation(A, F)
    check(not co.is_owned(A, F) and co.any_owned(A))
    for purpose, error in (("other", co.BridgeOwnedError), ("broadcast", co.BroadcastRefusedError)):
        try:
            with co.guarded_send(A, F, purpose=purpose):
                raise AssertionError("a reserved chat must refuse " + purpose)
        except error:
            check(True)
    check(rows("SELECT count(*) FROM twin_bridge_writes")[0][0] == 0, "a refused writer leaves no marker")

    # The bridge's own dispatch needs its attempt-bound token and publishes then clears a marker.
    try:
        with co.guarded_send(A, F, purpose="twin_bridge", dispatch_token="forged"):
            raise AssertionError("forged token")
    except co.DispatchNotAuthorized:
        check(True)
    token = co.authorize_dispatch(A, F, turn_id="t", attempt_no=1, owner_token="owner-a-000000000001", revision=1)
    with co.guarded_send(A, F, purpose="twin_bridge", dispatch_token=token):
        check(rows("SELECT state FROM twin_bridge_writes WHERE account_id=? AND fan_id=?", (A, F)) == [("held",)])
        check(co.in_flight_count(A, F) == 1)
    check(rows("SELECT count(*) FROM twin_bridge_writes")[0][0] == 0 and co.in_flight_count(A, F) == 0)
    co.revoke_dispatch(A, F, token)

    # An unreserved chat: an uncertain write keeps UNRESOLVED durable evidence that blocks reservation.
    G = F + 1
    seed_conversation(A, G)
    rows("UPDATE twin_bridge_conversations SET reserved=false,owner_token=NULL WHERE fan_id=?", (G,))
    try:
        with co.guarded_send(A, G, purpose="other"):
            raise RuntimeError("read timeout after bytes were sent")
    except RuntimeError:
        check(True)
    check(rows("SELECT state FROM twin_bridge_writes WHERE fan_id=?", (G,)) == [("unresolved",)])
    check(not await co.reserve_ownership(account_id=A, fan_id=G, owner_token="owner-c-000000000001", now=now,
                                         ttl_s=60, quiesce_timeout_s=0.1))
    check(co.reconcile_writes(account_id=A, fan_id=G) == 1)
    check(await co.reserve_ownership(account_id=A, fan_id=G, owner_token="owner-c-000000000001", now=now, ttl_s=60))
    check(rows("SELECT reserved,owner_token FROM twin_bridge_conversations WHERE fan_id=?", (G,))[0] == (True, "owner-c-000000000001"))
    co.clear_owner(A, G, "owner-c-000000000001")

    # A held marker whose holder stopped renewing is promoted to unresolved, not treated as clear.
    H = F + 2
    seed_conversation(A, H)
    rows("UPDATE twin_bridge_conversations SET reserved=false,owner_token=NULL WHERE fan_id=?", (H,))
    rows("INSERT INTO twin_bridge_writes(account_id,fan_id,holder,expires_at,state) VALUES(?,?,'dead','2000-01-01T00:00:00','held')", (A, H))
    check(co._reserve_atomic(A, H, "owner-d-000000000001", now, now + timedelta(seconds=60)) is False)
    check(rows("SELECT state FROM twin_bridge_writes WHERE holder='dead'") == [("unresolved",)])
    co.reconcile_writes(account_id=A)

    # Two processes race one free conversation: exactly one wins the reservation.
    K = F + 3
    seed_conversation(A, K)
    rows("UPDATE twin_bridge_conversations SET reserved=false,owner_token=NULL WHERE fan_id=?", (K,))
    results = []
    barrier = threading.Barrier(2)

    def contend(owner):
        barrier.wait()
        results.append(co._reserve_atomic(A, K, owner, now, now + timedelta(seconds=60)))

    threads = [threading.Thread(target=contend, args=(o,)) for o in ("owner-e-000000000001", "owner-f-000000000001")]
    [t.start() for t in threads]
    [t.join() for t in threads]
    check(sorted(results) == [False, True])

    # Releasing keeps the row and evidence; only the reservation flag clears.
    check(await co.release_ownership(account_id=A, fan_id=F, owner_token="owner-a-000000000001", now=now))
    check(rows("SELECT reserved,owner_token FROM twin_bridge_conversations WHERE fan_id=?", (F,))[0] == (False, None))
    reset()


def main():
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.exec_driver_sql(f'CREATE DATABASE "{NAME}" TEMPLATE template0 ENCODING \'UTF8\'')
    try:
        from db.schema import bootstrap
        engine = create_engine(SYNC_URL, connect_args={"options": "-c timezone=UTC"})
        with engine.begin() as conn:
            bootstrap(conn)
            # Employee ids the reused runner suite stamps on messages (Automation/human sentinels and a stray id);
            # messages.sent_by_employee_id is a real FK on PostgreSQL.
            conn.exec_driver_sql("INSERT INTO employees(id,display_name,is_active) VALUES "
                                 "(5,'Automation',false),(6,'Twin staff',false),(9,'Other',false)")
        engine.dispose()
        schema_checks()
        print("schema ok", CHECKS)

        # Reuse the SQLite suites' assertions verbatim on PostgreSQL.
        ledger_test.Fixture = PgFixture
        before = ledger_test.CHECKS
        with tempfile.TemporaryDirectory(prefix="native-pilot-pg-") as root:
            ledger_test.run(Path(root))
        print("ledger suite on PostgreSQL:", ledger_test.CHECKS - before, "checks")

        asyncio.run(ownership())
        print("ownership/coordination on PostgreSQL ok", CHECKS)

        ledger_test.database_url = lambda path: ASYNC_URL
        runner_test.CHECKS = 0
        with tempfile.TemporaryDirectory(prefix="native-pilot-pg-runner-") as root:
            asyncio.run(runner_test.suite(Path(root)))
        print("runner suite on PostgreSQL:", runner_test.CHECKS, "checks")
        print(f"native pilot storage on PostgreSQL: all cases passed ({CHECKS} own + suites)")
    finally:
        store.dispose_pools()
        with admin.connect() as c:
            c.exec_driver_sql(f'DROP DATABASE IF EXISTS "{NAME}" WITH (FORCE)')
        admin.dispose()


if __name__ == "__main__":
    main()
