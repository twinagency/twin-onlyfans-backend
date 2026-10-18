#!/usr/bin/env python3
"""OF-B1 — the OnlyFans migration, rehearsed on a real PostgreSQL.

Why this file exists at all: the OF-B1 suite proves the *binding rules* against
SQLite through the ORM's metadata, and SQLite accepts several things PostgreSQL
rejects outright. Both defects this repair is about were visible by READING the
DDL and not by running it — an INTEGER default on a BOOLEAN column, and a
`version_locations` string that Alembic's legacy split broke on the spaces in
this checkout's path — so "it compiles for SQLite" was never evidence. This is
the evidence.

WHAT IS REHEARSED, in this order on purpose:

  1. Alembic discovers the revision through its REAL configuration
     (`ScriptDirectory.from_config` on `service/db/migrations/alembic.ini`),
     which is exactly what the CLI does. It used to report ZERO revisions here.
  2. The parent revision is built the way a deployment builds it. The relay's
     boot path is `Base.metadata.create_all()`, and `alembic upgrade` runs
     afterwards for deltas only — CLAUDE.md records that rule, and the
     historical chain depends on it: several pre-0064 migrations call
     `op.create_table` on tables `create_all` has already made (for instance
     `20260519_0001_saved_replies`), so replaying the chain from empty is not a
     supported operation on any dialect. The parent schema is therefore created
     from the models MINUS this package's three tables, and alembic is stamped
     at `0064_user_llm_keys` — which is the state of a deployment that has been
     running the previous release.
  3. A clean `upgrade 0070_onlyfans_read_bindings` from there: a release. This is the first and only
     time this migration's own DDL runs, so the three tables below are genuinely
     its work rather than `create_all`'s.
  4. `downgrade 0064_user_llm_keys` — the migration is reversible.
  5. A SECOND `upgrade 0070_onlyfans_read_bindings`, which proves a re-run after a rollback.
  6. The constraint behaviour: at most one ACTIVE binding per account, rebinding
     after a revocation, and history that survives.

WHAT IT NEVER TOUCHES: nothing outside the scratch database it creates and
drops. `CHATTERLY_DB_URL` is set to the scratch URL for the duration, so
alembic's own default (`sqlite:///service/chatterly.db`, i.e. the working
checkout) is unreachable even by accident. No provider, no account, no
conversation, no send, no paid call.

HOW IT FINDS A SERVER, in order:

  • `ONLYFANS_TEST_PG_URL` — a `postgresql://` URL of a server it may create a
    database on. This is the CI arrangement (a postgres service container).
  • otherwise the bundled `pgserver` package if it is importable, which starts a
    throwaway server in a temp directory and stops it again. That is how this
    rehearsal was run for review. `pgserver` is NOT a project dependency.

With neither, it reports a loud SKIP and exits 0 — it has verified nothing, and
it says so, rather than failing a run that has no way to reach a PostgreSQL. It
deliberately does NOT print the "all cases passed" line in that state, so
`service/run_tests.sh`'s all-suites mode (which greps for that line) will show
this file as failed when no database is reachable. That is the honest reading:
the rehearsal did not run. Point `ONLYFANS_TEST_PG_URL` at a server, or install
`pgserver`, to have it actually verify something.

Run: python service/tests/test_onlyfans_migration_pg.py
     ONLYFANS_TEST_PG_URL=postgresql://user@host:5432/postgres python …
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import uuid

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SERVICE_ROOT = REPO_ROOT / "service"
ALEMBIC_INI = SERVICE_ROOT / "db" / "migrations" / "alembic.ini"
MIGRATION_REVISION = "0070_onlyfans_read_bindings"
PARENT_REVISION = "0064_user_llm_keys"
ACTIVE_INDEX = "uq_of_creator_bindings_active_account"

TABLES = ("of_creator_bindings", "of_conversation_state", "of_message_delivery")

# This historical rehearsal targets 0070 explicitly. The foundation suite
# separately exercises the current bootstrap and populated upgrades.
EXPECTED_HEAD = MIGRATION_REVISION

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        FAILURES.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def skip(reason: str) -> "None":
    print()
    print("-" * 72)
    print("SKIPPED - the PostgreSQL rehearsal did NOT run, so nothing was verified.")
    print(f"reason: {reason}")
    print("Set ONLYFANS_TEST_PG_URL, or `pip install pgserver`, to run it.")
    print("-" * 72)
    # Exit 0 so a runner with no database does not report a failure, and do NOT
    # print the "all cases passed" line: no case ran.
    sys.exit(0)


# ── A disposable server, and a scratch database on it ───────────────────────


def start_server() -> tuple[str, object | None]:
    """(admin URL, teardown callable). Exits with a SKIP when there is none."""
    explicit = (os.environ.get("ONLYFANS_TEST_PG_URL") or "").strip()
    if explicit:
        return explicit, None

    try:
        import pgserver  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001 — the message IS the skip reason
        skip(f"no ONLYFANS_TEST_PG_URL, and pgserver is not importable ({exc})")

    home = Path(tempfile.mkdtemp(prefix="of-b1-pg-"))
    print(f"  starting a disposable PostgreSQL under {home}")
    server = pgserver.get_server(str(home / "data"))
    return server.get_uri(), server


def stop_server(server: object | None) -> None:
    if server is None:
        return
    home = None
    try:
        pgdata = Path(str(server.pgdata))  # type: ignore[attr-defined]
        home = pgdata.parent
    except Exception:  # noqa: BLE001
        pass
    try:
        server.cleanup()  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 — teardown, not a result
        print(f"  note: pgserver cleanup raised {type(exc).__name__}: {exc}")
    if home is not None and home.exists() and home.name.startswith("of-b1-pg-"):
        shutil.rmtree(home, ignore_errors=True)


def make_scratch(admin_url: str):
    """(SQLAlchemy URL for a new empty database, a drop callable)."""
    import psycopg2
    from sqlalchemy.engine import make_url

    base = make_url(admin_url)
    name = f"of_b1_migration_{uuid.uuid4().hex[:8]}"

    admin = psycopg2.connect(base.set(drivername="postgresql").render_as_string(hide_password=False))
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
        cur.execute(f'CREATE DATABASE "{name}"')

    def drop() -> None:
        with admin.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
        admin.close()

    target = base.set(database=name).set(drivername="postgresql+psycopg2")
    return name, target.render_as_string(hide_password=False), drop


def connect(url: str):
    import psycopg2

    plain = url.replace("postgresql+psycopg2://", "postgresql://", 1)
    conn = psycopg2.connect(plain)
    conn.autocommit = True
    return conn


def scalar(conn, sql: str, params: tuple = ()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return row[0] if row else None


def rows(conn, sql: str, params: tuple = ()) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def present_tables(conn) -> list[str]:
    return sorted(
        table
        for table in TABLES
        if scalar(conn, "SELECT to_regclass(%s)", (f"public.{table}",)) is not None
    )


# ── The rehearsal ───────────────────────────────────────────────────────────


def rehearse(admin_url: str) -> int:
    scratch_name, scratch_url, drop_scratch = make_scratch(admin_url)
    print(f"  scratch database: {scratch_name}")

    # Point alembic at the scratch database and nothing else. env.py prefers
    # CHATTERLY_DB_URL over the ini's own sqlite default — the same precedence
    # the deployment uses — so this is the one knob that decides where the
    # migration lands.
    os.environ["CHATTERLY_DB_URL"] = scratch_url
    # Importing any migration pulls in `service.db.models`, and `service.db`
    # builds the relay's ASYNC engine from DATABASE_URL at import. Point that at
    # a throwaway SQLite file rather than at the scratch PostgreSQL: the async
    # engine is not what applies migrations (env.py uses a sync connection), and
    # a SQLite path that lives beside the pgserver data directory keeps this
    # process away from the working checkout's own `service/chatterly.db`.
    os.environ["DATABASE_URL"] = (
        f"sqlite+aiosqlite:///{(Path(tempfile.mkdtemp(prefix='of-b1-pg-async-')) / 'unused.db').as_posix()}"
    )

    sys.path.insert(0, str(REPO_ROOT))
    sys.path.insert(0, str(SERVICE_ROOT))

    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    # Imported here rather than at module scope: this whole file stays importable
    # (and runnable) without a PostgreSQL driver when it takes the skip path.
    import psycopg2

    cfg = Config(str(ALEMBIC_INI))

    # Source declarations are PostgreSQL compatible; no metadata shim.
    from service.db.models import Base
    from sqlalchemy import Boolean
    bad_defaults = [f"{t.name}.{c.name}" for t in Base.metadata.tables.values() for c in t.columns
                    if isinstance(c.type, Boolean) and c.server_default is not None
                    and str(c.server_default.arg).strip("'\"") in {"0", "1"}]
    check("all Boolean defaults are fixed at source", bad_defaults, [])

    # ── 1. Alembic's own discovery, through its own configuration ───────────
    print("\n1. Alembic discovers the revision through its real configuration")
    script = ScriptDirectory.from_config(cfg)
    discovered = [rev.revision for rev in script.walk_revisions()]
    check("the config resolves a non-empty revision graph", len(discovered) > 1, True)
    check("the OF-B1 revision is discovered", MIGRATION_REVISION in discovered, True)
    from db.schema import HEAD
    check("the supported runtime revision is the single head", list(script.get_heads()), [HEAD])
    check("its down_revision is the committed head it was cut from",
          script.get_revision(MIGRATION_REVISION).down_revision, PARENT_REVISION)
    check("0001_initial is still the single base", list(script.get_bases()), ["0001_initial"])

    conn = connect(scratch_url)
    check("the scratch database starts empty",
          scalar(conn, "SELECT count(*) FROM information_schema.tables "
                       "WHERE table_schema = 'public'"),
          0)

    # ── 2. The parent revision, built the way a deployment builds it ────────
    print("\n2. The parent schema (deployment boot path), stamped at 0064")
    from sqlalchemy import create_engine

    from service.db.models import Base as ModelsBase

    sync_engine = create_engine(scratch_url)
    ours = {name for name in ModelsBase.metadata.tables if name.startswith("of_")}
    check("the three new tables are the only ones this package adds",
          sorted(ours), sorted(TABLES))
    ModelsBase.metadata.create_all(
        sync_engine,
        tables=[
            table
            for name, table in ModelsBase.metadata.tables.items()
            if name not in ours
        ],
    )
    check("the parent schema is real - the relay's own tables were built",
          scalar(conn, "SELECT to_regclass('public.accounts')") is not None, True)
    check("and it does NOT contain this package's tables yet", present_tables(conn), [])
    command.stamp(cfg, PARENT_REVISION)
    check("alembic is stamped at the parent revision",
          scalar(conn, "SELECT version_num FROM alembic_version"), PARENT_REVISION)

    # ── 3. A clean upgrade from the parent — this is a release ──────────────
    print("\n3. A clean `upgrade 0070_onlyfans_read_bindings` from the parent revision")
    command.upgrade(cfg, MIGRATION_REVISION)
    check("alembic stamped the 0070 target it just built",
          scalar(conn, "SELECT version_num FROM alembic_version"), EXPECTED_HEAD)
    check("all three tables exist after the clean upgrade", present_tables(conn), sorted(TABLES))
    check("and are empty - the migration seeds nothing",
          [scalar(conn, f"SELECT count(*) FROM {table}") for table in TABLES], [0, 0, 0])
    # The parent schema excluded these three tables and the upgrade added them,
    # so what is on the server now is the MIGRATION's declaration. Spot-check the
    # one whose shape carries the whole repair.
    check("and their columns are the migration's own declaration",
          sorted(
              row[0]
              for row in rows(
                  conn,
                  "SELECT column_name FROM information_schema.columns "
                  "WHERE table_name = 'of_creator_bindings'",
              )
          ),
          sorted([
              "id", "tenant_id", "creator_id", "creator_name", "account_id",
              "handle", "channel", "created_at", "revoked_at",
          ]))

    # ── 4. Downgrade ────────────────────────────────────────────────────────
    print("\n4. `downgrade 0064_user_llm_keys`")
    command.downgrade(cfg, PARENT_REVISION)
    check("alembic stamped the parent revision",
          scalar(conn, "SELECT version_num FROM alembic_version"), PARENT_REVISION)
    check("every OF-B1 table is gone", present_tables(conn), [])
    check("and the relay's own tables were left alone",
          scalar(conn, "SELECT to_regclass('public.accounts')") is not None, True)

    # ── 5. The second clean upgrade ─────────────────────────────────────────
    print("\n5. A second clean `upgrade 0070_onlyfans_read_bindings` after the rollback")
    command.upgrade(cfg, MIGRATION_REVISION)
    check("it stamps the 0070 target again",
          scalar(conn, "SELECT version_num FROM alembic_version"), EXPECTED_HEAD)
    check("all three tables exist again", present_tables(conn), sorted(TABLES))

    # ── 6. The schema the migration itself wrote ────────────────────────────
    print("\n6. Keys, foreign keys and indexes, as PostgreSQL recorded them")
    for table in TABLES:
        check(f"{table} has a primary key",
              scalar(conn, "SELECT contype FROM pg_constraint "
                           "WHERE conrelid = %s::regclass AND contype = 'p'",
                     (f"public.{table}",)),
              "p")

    fk_rows = rows(
        conn,
        "SELECT conrelid::regclass::text, confrelid::regclass::text, confdeltype "
        "FROM pg_constraint WHERE contype = 'f' AND conrelid::regclass::text = ANY(%s)",
        (list(TABLES),),
    )
    check("each foreign key points where the models say, ON DELETE CASCADE",
          sorted(fk_rows),
          sorted([
              ("of_creator_bindings", "accounts", "c"),
              ("of_conversation_state", "fans", "c"),
              ("of_message_delivery", "messages", "c"),
          ]))

    index_names = (
        ACTIVE_INDEX,
        "ix_of_creator_bindings_tenant",
        "ix_of_creator_bindings_live",
        "ix_of_conversation_state_updated",
        "ix_of_message_delivery_attempt",
    )
    index_defs = {
        name: scalar(conn, "SELECT indexdef FROM pg_indexes WHERE indexname = %s", (name,))
        for name in index_names
    }
    check("every index the migration declares exists",
          sorted(name for name, definition in index_defs.items() if definition),
          sorted(index_names))
    active = index_defs[ACTIVE_INDEX] or ""
    check("the active-binding index is UNIQUE", "UNIQUE INDEX" in active, True)
    check("and PARTIAL - its WHERE clause is part of the index",
          "WHERE (revoked_at IS NULL)" in active, True)
    check("on the account column alone", "USING btree (account_id)" in active, True)
    check("the global UNIQUE(account_id) is not in PostgreSQL either",
          scalar(conn, "SELECT count(*) FROM pg_constraint "
                       "WHERE contype = 'u' AND conrelid = 'public.of_creator_bindings'::regclass"),
          0)

    check("the boolean column's default is PostgreSQL's own false, not 0",
          scalar(conn, "SELECT column_default FROM information_schema.columns "
                       "WHERE table_name = 'of_conversation_state' "
                       "AND column_name = 'requires_attention'"),
          "false")
    check("while the integer revision keeps its integer default",
          scalar(conn, "SELECT column_default FROM information_schema.columns "
                       "WHERE table_name = 'of_conversation_state' "
                       "AND column_name = 'revision'"),
          "0")

    # ── 7. The constraint behaviour ─────────────────────────────────────────
    print("\n7. One ACTIVE binding per account, and rebinding after a revocation")

    def bind(binding_id: str, account_id: str, *, tenant: str, creator: str,
             revoked: bool = False) -> str | None:
        """Insert a binding. The constraint name PostgreSQL refused, or None."""
        sql = (
            "INSERT INTO of_creator_bindings "
            "(id, tenant_id, creator_id, creator_name, account_id, handle, channel, revoked_at) "
            f"VALUES (%s, %s, %s, %s, %s, %s, 'onlyfans', {'now()' if revoked else 'NULL'})"
        )
        params = (binding_id, tenant, creator, creator, account_id, f"handle-{account_id}")
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
            return None
        except psycopg2.IntegrityError as exc:
            # The constraint PostgreSQL actually refused, by name — so the check
            # can prove it was THIS index (the partial one on the active
            # account) rather than, say, a primary key or a foreign key.
            return getattr(exc.diag, "constraint_name", None) or "integrity_error"

    for account_id in ("9001", "9002"):
        with conn.cursor() as cur:
            # `is_active_default` is NOT NULL with a PYTHON-side default only, so
            # `create_all` emits no database default for it and a raw INSERT has
            # to state it.
            cur.execute(
                "INSERT INTO accounts (id, is_active_default) VALUES (%s, false)",
                (account_id,),
            )

    check("a live binding is accepted",
          bind("ofb-9001-a", "9001", tenant="agency-a", creator="creator-ava"), None)
    check("a second ACTIVE binding for the same account is refused",
          bind("ofb-9001-b", "9001", tenant="agency-a", creator="creator-ava"), ACTIVE_INDEX)
    check("from another creator, too",
          bind("ofb-9001-c", "9001", tenant="agency-a", creator="creator-nova"), ACTIVE_INDEX)
    check("and from another tenant entirely",
          bind("ofb-9001-d", "9001", tenant="agency-b", creator="creator-bea"), ACTIVE_INDEX)
    check("so exactly one binding is live",
          scalar(conn, "SELECT count(*) FROM of_creator_bindings "
                       "WHERE account_id = '9001' AND revoked_at IS NULL"),
          1)

    with conn.cursor() as cur:
        cur.execute("UPDATE of_creator_bindings SET revoked_at = now() WHERE id = 'ofb-9001-a'")
    check("after a revocation the SAME tenant and creator can be bound again",
          bind("ofb-9001-e", "9001", tenant="agency-a", creator="creator-ava"), None)
    with conn.cursor() as cur:
        cur.execute("UPDATE of_creator_bindings SET revoked_at = now() WHERE id = 'ofb-9001-e'")
    check("and a DIFFERENT creator can take the account over once it is free",
          bind("ofb-9001-f", "9001", tenant="agency-a", creator="creator-nova"), None)

    check("history is intact: every revoked row is still there, with its owner",
          rows(conn, "SELECT id, tenant_id, creator_id FROM of_creator_bindings "
                     "WHERE account_id = '9001' AND revoked_at IS NOT NULL ORDER BY id"),
          [("ofb-9001-a", "agency-a", "creator-ava"),
           ("ofb-9001-e", "agency-a", "creator-ava")])
    check("and the live row is the newest, with its new owner",
          rows(conn, "SELECT id, creator_id FROM of_creator_bindings "
                     "WHERE account_id = '9001' AND revoked_at IS NULL ORDER BY id"),
          [("ofb-9001-f", "creator-nova")])
    check("history is unbounded - three rows now exist for one account",
          scalar(conn, "SELECT count(*) FROM of_creator_bindings WHERE account_id = '9001'"),
          3)

    check("a binding inserted ALREADY revoked is accepted",
          bind("ofb-9002-a", "9002", tenant="agency-a", creator="creator-ava", revoked=True),
          None)
    check("and a revoked-only account has no live binding",
          scalar(conn, "SELECT count(*) FROM of_creator_bindings "
                       "WHERE account_id = '9002' AND revoked_at IS NULL"),
          0)

    # ── 8. Tear the scratch database down ───────────────────────────────────
    print("\n8. The scratch database is dropped")
    conn.close()
    drop_scratch()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED")
        for failure in FAILURES:
            print(f"   - {failure}")
        return 1
    print("all cases passed - OnlyFans migration rehearsal (OF-B1, PostgreSQL)")
    return 0


def main() -> int:
    admin_url, server = start_server()
    try:
        return rehearse(admin_url)
    finally:
        stop_server(server)


if __name__ == "__main__":
    sys.exit(main())
