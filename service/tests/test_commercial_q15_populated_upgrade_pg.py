"""Q15: populated historical upgrades from every documented entry point, through 0138, verified table by table.

SYNTHETIC rows only (no production snapshot, no provider, no model, no network). Every database is built from the REAL migration files,
never stamped past missing DDL. For each historical start the lineage test already proves (0129, 0130..0133 of the original lineage and
0129b of the current lineage) the database is first populated with meaningful commercial evidence (pilot scope counters, a retained
reconnect receipt, delivery attempts of every outcome including an UNKNOWN send, model calls of every status including an unknown one
with its reservation), and then EVERY remaining table that exists is given one synthetic row, so nothing in the schema is upgraded empty.
It is upgraded through BOTH documented entry points (`db.schema.upgrade`, i.e. `python -m db.schema upgrade`, and plain
`alembic upgrade head`) and the result must keep every original row byte-for-byte on its original columns, keep the row counts,
carry the constraints/guards that `check_schema` pins at 0138, and still refuse what the guards refuse (no evidence rewritten by the
upgrade, unknown sends and reservations unchanged).

Tables whose multi-column CHECK constraints the generic filler cannot satisfy are listed in KNOWN_UNFILLED and each is covered by its
own migration test (see the comment); the test fails if that set grows silently or if a REQUIRED evidence table is left empty.
"""
import json
import re

import pytest
from alembic import command
from sqlalchemy import text

from db import schema as S
from test_commercial_retry_due_lineage_pg import (STARTS, NOW, _assert_complete, _assert_rows_preserved, _build, _populate, _snapshot, blank)  # noqa: F401

# Evidence categories the handoff names, and the table that holds each. All must be populated before the upgrade.
REQUIRED = {
    "sends": ("of_engine_attempt", "of_text_intent", "of_message_delivery"),
    "purchases": ("of_commerce_evidence", "transactions", "of_engine_access_evidence"),
    "retry state": ("of_engine_model_call", "of_vault_media_job"),
    "grants": ("of_engine_offer_grant", "of_engine_decision"),
    "counters": ("of_engine_pilot_scope", "of_delivery_policy", "of_provider_read_budget"),
    "reservations": ("of_engine_model_call", "of_engine_request"),
}
# Multi-column CHECKs the generic filler cannot satisfy; none holds sends, purchases, grants, counters or reservations.
KNOWN_UNFILLED = {"blacklist", "of_content_change", "of_content_sync_draft", "of_engagement_action", "of_engine_pilot_reconnect_receipt",
                  "of_ledger_retry_command", "of_offer_import_decision", "of_offer_observation", "of_personality_revision",
                  "of_vault_sync_job"}


def _fill_generic(c, table):
    cols = c.execute(text("SELECT column_name,data_type,is_nullable,column_default,character_maximum_length FROM information_schema.columns "
                          "WHERE table_schema='public' AND table_name=:t ORDER BY ordinal_position"), {"t": table}).all()
    checks = [r[0] for r in c.execute(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid=CAST(:t AS regclass) AND contype='c'"),
                                      {"t": table})]
    row = {}
    for name, dtype, nullable, default, maxlen in cols:
        if nullable == "YES" or default is not None:
            continue
        allowed = None
        for definition in checks:
            m = re.search(r"\(?" + re.escape(name) + r"\)?(?:::[a-z ]+)?\s*=\s*ANY \(+ARRAY\[((?:'[^']*'(?:::[a-z ]+)?(?:, )?)+)", definition)
            if m:
                allowed = re.findall(r"'([^']*)'", m.group(1))[0]
                break
        if allowed is not None:
            row[name] = allowed
        elif dtype in ("integer", "bigint", "smallint", "double precision", "numeric", "real"):
            row[name] = 1
        elif dtype == "boolean":
            row[name] = False
        elif dtype.startswith("timestamp"):
            row[name] = NOW
        elif dtype == "date":
            row[name] = NOW.date()
        elif dtype == "bytea":
            row[name] = b"x"
        elif dtype in ("json", "jsonb"):
            row[name] = "{}"
        else:
            row[name] = ("v-" + name)[: maxlen or 200]
    if row:
        c.execute(text(f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join(':' + k for k in row)})"), row)
    else:
        c.execute(text(f"INSERT INTO {table} DEFAULT VALUES"))


def _populate_everything(engine):
    """_populate()'s meaningful rows first; then one synthetic row in every other table. Returns (filled, unfilled)."""
    _populate(engine)
    with engine.connect() as c:
        tables = [r[0] for r in c.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema='public' "
                                               "AND table_type='BASE TABLE' AND table_name<>'alembic_version' ORDER BY 1"))]
    filled, unfilled = [], []
    for table in tables:
        with engine.connect() as c:
            tx = c.begin()
            try:
                c.execute(text("SET LOCAL session_replication_role = replica"))  # synthetic rows: the parents are not the subject
                if c.scalar(text(f"SELECT EXISTS (SELECT 1 FROM {table})")):
                    filled.append(table)
                    tx.rollback()
                    continue
                _fill_generic(c, table)
                tx.commit()
                filled.append(table)
            except Exception:
                tx.rollback()
                unfilled.append(table)
    return filled, unfilled


def _all_rows(engine, tables):
    with engine.begin() as c:
        return {t: sorted(r[0] for r in c.execute(text(f"SELECT to_jsonb(x)::text FROM {t} x"))) for t in tables}


def _assert_everything_kept(before, after):
    for table, rows in before.items():
        assert len(after[table]) == len(rows), f"{table}: row count changed {len(rows)} -> {len(after[table])}"
        remaining = [json.loads(r) for r in after[table]]
        for old_text in rows:
            old = json.loads(old_text)
            match = next((i for i, new in enumerate(remaining) if all(new.get(k) == v for k, v in old.items())), None)
            assert match is not None, f"{table}: an original row was altered or lost by the upgrade: {old_text[:200]}"
            remaining.pop(match)


@pytest.mark.parametrize("entry", ["db.schema.upgrade", "alembic upgrade head"])
@pytest.mark.parametrize("lineage,start", STARTS)
def test_every_table_populated_upgrades_to_0138_keeping_every_row(blank, tmp_path, lineage, start, entry):
    _build(blank, tmp_path, start, lineage=lineage)
    filled, unfilled = _populate_everything(blank)
    assert set(unfilled) <= KNOWN_UNFILLED, f"tables that could not be populated and are not on the known list: {sorted(set(unfilled) - KNOWN_UNFILLED)}"
    for category, tables in REQUIRED.items():
        for table in tables:
            assert table in filled, f"{category}: {table} was not populated before the upgrade"
    before_tables = _all_rows(blank, filled)
    before_special = _snapshot(blank)
    assert len(filled) >= 170 and all(before_tables[t] for t in filled), len(filled)   # the schema is not upgraded empty
    with blank.begin() as c:
        if entry == "db.schema.upgrade":
            S.upgrade(c)
        else:
            command.upgrade(S._config(c), "head")
    _assert_complete(blank)                        # head 0138, check_schema, R0 columns/constraints/guard digest
    _assert_rows_preserved(before_special, _snapshot(blank))   # unknown send, reservation, counter, receipt unchanged
    after_tables = _all_rows(blank, filled)
    _assert_everything_kept(before_tables, after_tables)
    # the upgrade is idempotent and never rewrites the evidence it just carried
    with blank.begin() as c:
        S.upgrade(c)
    assert _all_rows(blank, filled) == after_tables


def test_the_row_comparison_is_not_vacuous():
    row = json.dumps({"a": 1, "b": "x"})
    _assert_everything_kept({"t": [row]}, {"t": [json.dumps({"a": 1, "b": "x", "new_column": None})]})  # added columns are fine
    for after in ([json.dumps({"a": 1, "b": "changed"})], [], [row, row]):
        with pytest.raises(AssertionError):
            _assert_everything_kept({"t": [row]}, {"t": after})
