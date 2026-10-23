"""
Alembic environment. Run by `alembic upgrade head` (CLI) or by
service/db/engine.py:init_db() at relay startup.

We hand Alembic our own MetaData object (service.db.models.Base.metadata)
so `--autogenerate` can diff the live schema against the source-of-truth
Python models.
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

# Python 3.13 + SQLAlchemy 2.x autogenerate hits CPython's default 1000 limit
# on wide schemas (35 tables with composite PKs + partial indexes here).
# The actual call depth is ~1100; bumping to 5000 is comfortably safe and
# only affects this Alembic process, not the relay's runtime.
sys.setrecursionlimit(50000)

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import make_url

# Make sibling `service/` importable when alembic is invoked from anywhere.
HERE = Path(__file__).resolve().parent
SERVICE_ROOT = HERE.parent.parent
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(SERVICE_ROOT.parent))  # so `service.db.models` resolves too

from service.db import onlyfans_models  # noqa: F401 — register foundation metadata
from service.db import onlyfans_delivery_models  # noqa: F401 — register synthetic delivery metadata
from service.db import onlyfans_campaign_models,onlyfans_connection_models,onlyfans_content_models  # noqa: F401
from service.db import onlyfans_vault_models, onlyfans_ledger_models, onlyfans_creator_facts_models, onlyfans_vault_edit_models  # noqa: F401
from service.db.models import Base  # noqa: E402  — must come after sys.path patch

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _resolve_url() -> str:
    """Pick the DB URL with explicit, loud overrides so we never hit prod
    by accident again.

    Precedence:
      1. `-x url=...` on the alembic CLI (per-invocation override)
      2. CHATTERLY_DB_URL env var (shell-scoped override)
      3. alembic.ini sqlalchemy.url (default; usually prod sqlite)

    Whichever wins is logged to stderr BEFORE any DB op so a misrouted
    command is visible immediately. Past incident (2026-05-22): `-x url=`
    was silently ignored because this function didn't exist; prod was
    metadata-stamped while the operator thought they were hitting a copy.
    """
    x_args = context.get_x_argument(as_dictionary=True)
    if "url" in x_args:
        url, source = x_args["url"], "-x url="
    elif os.environ.get("CHATTERLY_DB_URL"):
        url, source = os.environ["CHATTERLY_DB_URL"], "CHATTERLY_DB_URL env"
    else:
        url, source = config.get_main_option("sqlalchemy.url"), "alembic.ini default"
    print(f"[alembic env] using DB URL: {make_url(url).render_as_string(hide_password=True)}  (source: {source})", file=sys.stderr)
    return url


def run_migrations_offline() -> None:
    """Generate SQL without a live DB connection. Used by `alembic upgrade --sql`."""
    url = _resolve_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite ALTER is limited — render with batch mode so future schema
        # changes (e.g. adding a column) automatically use the
        # recreate-table-and-copy strategy.
        render_as_batch=url.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Standard online migration against a live DB."""
    def run(connection):
        context.configure(connection=connection, target_metadata=target_metadata,
                          render_as_batch=connection.dialect.name == "sqlite")
        with context.begin_transaction():
            context.run_migrations()

    supplied = config.attributes.get("connection")
    if supplied is not None:
        run(supplied)
        return
    section = config.get_section(config.config_ini_section, {}) or {}
    section["sqlalchemy.url"] = _resolve_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    try:
        with connectable.connect() as connection:
            run(connection)
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
