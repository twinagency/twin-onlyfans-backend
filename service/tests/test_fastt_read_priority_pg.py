"""Background provider reads (history import, exporter, discovery, vault analysis) yield to live replies on the shared lease."""
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database  # noqa: F401  (fixture)
from test_onlyfans_launch_provider_budget_pg import native  # noqa: F401  (fixture)
from test_onlyfans_connections_pg import TENANT
from db.onlyfans_content_sync_progress_models import OfProviderReadBudget
from onlyfans_runtime.provider_read_budget import acquire, release, ReadDeferred
from onlyfans_runtime.store import utcnow


def prepare(store, account, tokens):
    from db.onlyfans_pilot_models import ensure_tables
    ensure_tables(store.engine)
    with Session(store.engine) as s, s.begin():
        row = s.get(OfProviderReadBudget, account)
        if row is None:
            acquire(store, TENANT, account, connection_revision=1, kind="recent")
            release(store, TENANT, acquire(store, TENANT, account, connection_revision=1, kind="recent"), retry_after=None) if False else None
    with Session(store.engine) as s, s.begin():
        row = s.get(OfProviderReadBudget, account)
        row.tokens, row.refilled_at, row.lease_token, row.lease_until, row.backoff_until = tokens, utcnow(), None, None, None


def set_board(store, account, state):
    with store.engine.begin() as c:
        c.execute(text("DELETE FROM fastt_runtime.engine_work"))
        if state:
            now = utcnow().isoformat(sep=" ", timespec="microseconds")
            c.execute(text("INSERT INTO fastt_runtime.engine_work(account_id,fan_id,state,reason,state_since,attempts,updated_at) "
                           "VALUES(:a,7,:s,NULL,:n,0,:n)"), {"a": account, "s": state, "n": now})


def test_history_and_vault_yield_while_replies_wait_but_intake_and_money_do_not(native):
    store, account, reader = native
    prepare(store, account, tokens=6)
    set_board(store, account, None)
    claim = acquire(store, TENANT, account, connection_revision=1, kind="history")      # no reply waiting: 6 > 3
    release(store, TENANT, claim)
    prepare(store, account, tokens=6)
    set_board(store, account, "preparing")
    with pytest.raises(ReadDeferred):
        acquire(store, TENANT, account, connection_revision=1, kind="history")           # 6 <= 3 + 4: yields
    with pytest.raises(ReadDeferred):
        acquire(store, TENANT, account, connection_revision=1, kind="vault")
    release(store, TENANT, acquire(store, TENANT, account, connection_revision=1, kind="recent"))   # live intake backfill: unaffected
    prepare(store, account, tokens=6)
    release(store, TENANT, acquire(store, TENANT, account, connection_revision=1, kind="ledger"))   # payments: unaffected
    prepare(store, account, tokens=9)
    release(store, TENANT, acquire(store, TENANT, account, connection_revision=1, kind="history"))  # a trickle still gets through
    # a stale board (the engine died) does not hold background reads back for ever
    with store.engine.begin() as c:
        c.execute(text("UPDATE fastt_runtime.engine_work SET updated_at=:old"),
                  {"old": (utcnow() - timedelta(minutes=10)).isoformat(sep=" ", timespec="microseconds")})
    prepare(store, account, tokens=6)
    set_board(store, account, None)
