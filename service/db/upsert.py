"""Dialect-aware INSERT ... ON CONFLICT for the FASTT write path.

FASTT was written for SQLite and builds every upsert with
`sqlalchemy.dialects.sqlite.insert`. The Twin runtime stores history on
PostgreSQL, where that construct compiles to `INSERT OR ...` style SQL the
server rejects. The two dialect `insert()` constructs expose the same
`on_conflict_do_update(index_elements=, set_=, where=)` / `on_conflict_do_nothing`
/ `.excluded` surface, so the statements themselves do not change: only the
constructor does. FASTT modules import this under their historical name
(`sqlite_insert`) so the diff to upstream stays one line per file and the
selling/stop logic is untouched.

The dialect is read from the bind that will execute the statement (a Session,
AsyncSession, Engine or Connection), else from the process engine in
`db.engine`.
"""
from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as _postgresql_insert
from sqlalchemy.dialects.sqlite import insert as _sqlite_insert


def _dialect_name(bind) -> str:
    if bind is None:
        from db.engine import engine
        return engine.dialect.name
    if isinstance(bind, str):
        return bind
    if hasattr(bind, "get_bind"):          # Session / AsyncSession
        bind = bind.get_bind()
    return bind.dialect.name


def dialect_insert(model, bind=None):
    """`insert(model)` for the dialect that will run it (postgresql or sqlite)."""
    return (_postgresql_insert if _dialect_name(bind) == "postgresql" else _sqlite_insert)(model)
