"""Synchronous raw-SQL access to the coordination store: SQLite file OR PostgreSQL.

The send boundary (``coordination.guarded_send``) and the native pilot ledger are
synchronous and use short, explicit transactions; they cannot await an async
session. They were written against SQLite (``BEGIN IMMEDIATE`` serialises every
writer). This module keeps their SQL text unchanged and supplies the same
guarantees on PostgreSQL:

* ``BEGIN IMMEDIATE`` takes one transaction-scoped advisory lock, so every
  "read the guards, then write" admission is serialised exactly as SQLite does.
  The lock is released by COMMIT/ROLLBACK; nothing is held across a network call.
* ``?`` placeholders, tuple-like rows with name access, ``lastrowid`` (via
  ``insert_id``) and naive-UTC timestamps rendered as ISO strings are identical on
  both backends, so comparisons and hashes behave the same.

A *target* is either a SQLite file path or a PostgreSQL URL string. Importing
this module imports no application code and opens nothing.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

# One lock for every coordination / pilot-ledger write transaction.
PG_LOCK_KEY = 7811092
_PG_OPTIONS = "-c timezone=UTC -c lock_timeout=5000"

try:  # psycopg2 is an application dependency; SQLite-only tools may lack it.
    import psycopg2
    DB_ERRORS: tuple = (sqlite3.Error, psycopg2.Error)
except ImportError:  # pragma: no cover
    psycopg2 = None
    DB_ERRORS = (sqlite3.Error,)


def is_pg(target: object) -> bool:
    return isinstance(target, str) and target.split(":", 1)[0].startswith("postgresql")


def pg_url(url: str) -> str:
    """The synchronous psycopg2 form of an application DATABASE_URL."""
    from sqlalchemy.engine import make_url
    return make_url(url).set(drivername="postgresql+psycopg2").render_as_string(hide_password=False)


def target_from_url(url: str | None):
    """Map an application DATABASE_URL to a target: PG URL string, SQLite path, or None."""
    if not url:
        return None
    if url.startswith("postgres"):
        return pg_url(url)
    if not url.startswith("sqlite"):
        return None
    path = url
    for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
        if path.startswith(prefix):
            path = path[len(prefix):]
            break
    if path.startswith("file:"):
        return None
    return path or None


def iso(value: datetime) -> str:
    """Naive-UTC ISO text, the shape SQLite already stores for these columns."""
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat(sep=" ", timespec="microseconds")


def _plain(value):
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, memoryview):
        return bytes(value)
    return value


class Row(tuple):
    """Tuple that also answers ``row["name"]``, ``keys()`` and ``dict(row)``."""
    def __new__(cls, keys, values):
        obj = super().__new__(cls, values)
        obj._keys = tuple(keys)
        return obj

    def keys(self):
        return list(self._keys)

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                return tuple.__getitem__(self, self._keys.index(key))
            except ValueError:
                raise IndexError("No item with that key") from None
        return tuple.__getitem__(self, key)


class _Result:
    def __init__(self, rows, rowcount):
        self._rows, self.rowcount, self._at = rows, rowcount, 0

    def fetchone(self):
        if self._at >= len(self._rows):
            return None
        self._at += 1
        return self._rows[self._at - 1]

    def fetchall(self):
        rows, self._at = self._rows[self._at:], len(self._rows)
        return rows

    def __iter__(self):
        return iter(self.fetchall())


class PgConnection:
    """The small slice of the sqlite3.Connection surface the coordination code uses."""

    def __init__(self, raw):
        self._raw = raw
        self.in_transaction = False

    def execute(self, sql: str, params=()):
        word = " ".join(sql.split()).upper()
        if word == "BEGIN IMMEDIATE":
            cur = self._raw.cursor()
            try:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (PG_LOCK_KEY,))
            finally:
                cur.close()
            self.in_transaction = True
            return _Result([], 0)
        if word == "BEGIN":
            return _Result([], 0)
        if word == "COMMIT":
            return self._end(True)
        if word == "ROLLBACK":
            return self._end(False)
        if word.startswith("SET TRANSACTION"):
            # Must be the first statement of a transaction (read-only snapshot).
            self._raw.rollback()
        text = sql.replace("%", "%%").replace("?", "%s") if params else sql
        cur = self._raw.cursor()
        try:
            cur.execute(text, tuple(params) if params else None)
            self.in_transaction = True
            rows = []
            if cur.description:
                keys = [d[0] for d in cur.description]
                rows = [Row(keys, [_plain(v) for v in r]) for r in cur.fetchall()]
            return _Result(rows, cur.rowcount)
        finally:
            cur.close()

    def _end(self, commit: bool):
        try:
            (self._raw.commit if commit else self._raw.rollback)()
        finally:
            self.in_transaction = False
        return _Result([], 0)

    def commit(self):
        self._end(True)

    def rollback(self):
        self._end(False)

    def close(self):
        try:
            self._raw.close()  # a pooled connection is rolled back and returned
        except Exception:  # noqa: BLE001
            pass


_ENGINES: dict[str, object] = {}
_ENGINES_LOCK = threading.Lock()


def _engine(url: str):
    with _ENGINES_LOCK:
        eng = _ENGINES.get(url)
        if eng is None:
            from sqlalchemy import create_engine
            eng = create_engine(url, pool_size=2, max_overflow=4, pool_pre_ping=True,
                                connect_args={"options": _PG_OPTIONS})
            _ENGINES[url] = eng
        return eng


def dispose_pools() -> None:
    """Close pooled PostgreSQL connections (tests that drop their database)."""
    with _ENGINES_LOCK:
        engines = list(_ENGINES.values())
        _ENGINES.clear()
    for eng in engines:
        eng.dispose()


def connect_pg(url: str) -> PgConnection:
    return PgConnection(_engine(url).raw_connection())


def connect_sqlite(path, *, mode: str = "rw", timeout: float = 5.0) -> sqlite3.Connection:
    """Explicit-transaction SQLite connection that never creates a missing file."""
    con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=" + mode, uri=True,
                          timeout=timeout, isolation_level=None)
    con.row_factory = sqlite3.Row
    return con


def has_table(con, name: str) -> bool:
    if isinstance(con, PgConnection):
        return bool(con.execute("SELECT 1 FROM pg_catalog.pg_tables WHERE schemaname=current_schema() "
                                "AND tablename=?", (name,)).fetchone())
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def insert_id(con, sql: str, params=()) -> int:
    """Run an INSERT into a table with an ``id`` key and return the new id."""
    if isinstance(con, PgConnection):
        return int(con.execute(sql + " RETURNING id", params).fetchone()[0])
    return int(con.execute(sql, params).lastrowid)


@contextmanager
def transaction(target):
    """Serialised write transaction (BEGIN IMMEDIATE); commits on success."""
    con = connect_pg(target) if is_pg(target) else connect_sqlite(target)
    try:
        con.execute("BEGIN IMMEDIATE")
        yield con
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()


@contextmanager
def readonly(target):
    """One consistent read-only snapshot; never writes, always rolled back."""
    if is_pg(target):
        con = connect_pg(target)
        try:
            con.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            yield con
        finally:
            try:
                con.rollback()
            finally:
                con.close()
        return
    con = connect_sqlite(target, mode="ro")
    try:
        con.execute("PRAGMA query_only=ON")
        con.execute("BEGIN")
        yield con
    finally:
        try:
            if con.in_transaction:
                con.execute("ROLLBACK")
        finally:
            con.close()


def dialect_insert(model):
    """``INSERT ... ON CONFLICT`` builder for the application database's dialect."""
    from db.engine import engine
    if engine.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert(model)
