"""0129b_model_retry_due on the commercial graph: populated upgrades from every historical starting lineage.

The commercial branch re-parented 0130 onto 0129b (R0). A database created by the ORIGINAL lineage (0129 -> 0130 ...) that
is already at 0130..0133 (or was raw-upgraded to 0136) never ran 0129b, so it lacks of_engine_model_call.retry_not_before,
failure_detail_json, their two checks and the R0 guard body, while claiming a revision whose ancestors include 0129b.
0137_model_retry_due_repair completes exactly that missing DDL, data-preserving and idempotent; db.schema.upgrade accepts such a
database only when it matches the old lineage exactly. The old lineage is rebuilt from the REAL migration files: the migrations
directory with the single historical difference (0130's parent) and without every revision added after the re-parenting.

Synthetic rows only. No provider, no network.
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from db import schema as S
from db.schema import HEAD, SchemaMismatch, check_schema

REPAIR = "0137_model_retry_due_repair"
R0_GUARD_DIGEST = "ab95da609ffe3d4915fd52aab42c61a99cd56649bac3e345ee34b41b61508cf5"
PRE_R0_GUARD_DIGEST = "5630b811cd298311ff01ddcc0314a176be308b5d56873c50ebd780d0bdd433d8"
NOW = datetime.datetime(2026, 11, 11, 3, 0, 0)
H = lambda s: hashlib.sha256(s.encode()).hexdigest()  # noqa: E731
MIGRATIONS = S.HERE / "migrations"
ORIGINAL_LINEAGE_REVISIONS = {"0129b_model_retry_due", "0134_commercial_scope_contract", "0135_commercial_catalogue_terms",
                              "0136_history_coverage", REPAIR, "0138_tip_evidence_kind"}


def _old_lineage_config(tmp_path, conn):
    """The migrations directory as it was before 0129b was inserted: 0130's parent is 0129 and nothing after 0133 exists."""
    root = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS, root, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(S.HERE / "snapshots", tmp_path / "snapshots")  # migrations read ../snapshots
    patched = False
    for path in (root / "versions").glob("*.py"):
        body = path.read_text()
        revision = re.search(r"^revision\s*(?::[^=\n]+)?=\s*([^\n#]+)", body, re.M).group(1)
        if any(f'"{r}"' in revision or f"'{r}'" in revision for r in ORIGINAL_LINEAGE_REVISIONS):
            path.unlink()
        elif '"0130_commercial_vault_provenance"' in revision:
            assert 'down_revision="0129b_model_retry_due"' in body
            path.write_text(body.replace('down_revision="0129b_model_retry_due"', 'down_revision="0129_reconnect_receipt"'))
            patched = True
    assert patched
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root))
    cfg.attributes["connection"] = conn
    return cfg


@pytest.fixture
def blank():
    admin_url = os.environ["ONLYFANS_TEST_PG_URL"]
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    name = "of_lineage_" + uuid.uuid4().hex
    with admin.connect() as c:
        c.exec_driver_sql(f'CREATE DATABASE "{name}" TEMPLATE template0 ENCODING \'UTF8\'')
    engine = create_engine(admin.url.set(database=name).render_as_string(hide_password=False),
                           connect_args={"options": "-c timezone=UTC"})
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
        admin.dispose()


def _build(engine, tmp_path, target, *, lineage):
    """Real migration files, never a stamp past missing DDL."""
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(7811070)"))
        conn.exec_driver_sql((S.HERE / "snapshots/0070_postgresql.sql").read_text())
        command.stamp(S._config(conn), S.BASELINE)
        cfg = _old_lineage_config(tmp_path, conn) if lineage == "original" else S._config(conn)
        command.upgrade(cfg, target)


def _fill(c, table, over):
    cols = c.execute(text("SELECT column_name,data_type,is_nullable,column_default FROM information_schema.columns "
                          "WHERE table_schema='public' AND table_name=:t ORDER BY ordinal_position"), {"t": table}).all()
    row = {k: v for k, v in over.items() if k in {r[0] for r in cols}}
    for name, dtype, nullable, default in cols:
        if name in row or nullable == "YES" or default is not None:
            continue
        row[name] = (1 if dtype in ("integer", "bigint", "smallint") else False if dtype == "boolean"
                     else NOW if dtype.startswith("timestamp") else b"x" if dtype == "bytea"
                     else "{}" if dtype in ("json", "jsonb") else "v-" + name)
    c.execute(text(f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join(':' + k for k in row)})"), row)


TABLES = {"of_engine_pilot_scope": "scope_id", "of_engine_model_call": "call_id", "of_engine_attempt": "attempt_id",
          "of_engine_pilot_webhook_digest": "event_key", "of_engine_pilot_reconnect_receipt": "event_key",
          "of_engine_request": "request_id"}


def _populate(engine):
    """Counters, a retained reconnect receipt, delivery attempts and every model-call outcome, incl. an unknown one."""
    a, b = "acct-lineage", "bind-lineage"
    with engine.begin() as c:
        c.execute(text("SET LOCAL session_replication_role = replica"))  # synthetic rows; parents are not the subject
        _fill(c, "of_engine_pilot_scope", dict(scope_id=1, account_id=a, binding_id=b, revision=3, status="active", active_since=NOW,
              config_json=json.dumps({"fans": [101, 102]}), fingerprint=H("scope"), provider_requests_used=14,
              provider_credits_reserved=9, created_at=NOW, updated_at=NOW))
        _fill(c, "of_engine_request", dict(account_id=a, request_id="00000000-0000-0000-0000-000000000101", fan_id=101, binding_id=b,
              status="open", owner="engine", next_step="prepare_reply", created_at=NOW, updated_at=NOW))
        for n, status, outcome, actual, known in [("1", "settled", "ok", 311, True), ("2", "unknown", "unknown", None, False),
                                                    ("3", "released", "not_sent", None, False), ("4", "reserved", None, None, False),
                                                    ("5", "calling", None, None, False)]:
            _fill(c, "of_engine_model_call", dict(account_id=a, call_id=f"00000000-0000-0000-0000-0000000001{n}0",
                  request_id="00000000-0000-0000-0000-000000000101", generation=1, seq=int(n), chain_pos=0, chain_step="primary",
                  fan_id=101, binding_id=b, owner_epoch=1, provider="openrouter", model="m/primary", prefixed=False, status=status,
                  outcome=outcome, reserved_microusd=400, actual_microusd=actual, usage_known=known, lease_owner="w1",
                  lease_expires_at=NOW, attempt=1, snapshot_sha256=H("s" + n), prompt_sha256=H("p" + n), cost_day=NOW.date(),
                  created_at=NOW, updated_at=NOW))
        for n, outcome in (("1", "unknown"), ("2", "accepted"), ("3", "not_accepted")):
            _fill(c, "of_engine_attempt", dict(account_id=a, attempt_id=f"00000000-0000-0000-0000-0000000002{n}0",
                  intent_id="00000000-0000-0000-0000-000000000301", outcome=outcome, payload_hash=H("pl" + n),
                  evidence_json=json.dumps({"provider_message_id": "m" + n}), started_at=NOW, updated_at=NOW))
        _fill(c, "of_engine_pilot_webhook_digest", dict(account_id=a, epoch=2, event_key="evt-1", payload_sha256=H("body"), received_at=NOW))
        _fill(c, "of_engine_pilot_reconnect_receipt", dict(account_id=a, epoch=2, event_key="evt-1", payload_sha256=H("body"),
              raw_body=b'{"synthetic":true}', signature=H("sig"), received_at=NOW))


def _snapshot(engine):
    with engine.begin() as c:
        return {t: [json.loads(r[0]) for r in c.execute(text(f"SELECT to_jsonb(x)::text FROM {t} x ORDER BY {k}"))] for t, k in TABLES.items()}


def _assert_rows_preserved(before, after):
    for table, rows in before.items():
        assert len(after[table]) == len(rows), table
        for old, new in zip(rows, after[table]):
            assert {k: new.get(k) for k in old} == old, table
    unknown = [r for r in after["of_engine_model_call"] if r["status"] == "unknown"]
    assert len(unknown) == 1 and unknown[0]["reserved_microusd"] == 400 and unknown[0]["retry_not_before"] is None
    assert [r["outcome"] for r in after["of_engine_attempt"]] == ["unknown", "accepted", "not_accepted"] or \
        sorted(r["outcome"] for r in after["of_engine_attempt"]) == ["accepted", "not_accepted", "unknown"]
    assert after["of_engine_pilot_scope"][0]["provider_requests_used"] == 14
    assert len(after["of_engine_pilot_reconnect_receipt"]) == 1


def _guard_digest(conn):
    src = conn.scalar(text("SELECT p.prosrc FROM pg_proc p WHERE p.proname='of_engine_model_call_guard'"))
    return hashlib.sha256(" ".join(src.split()).encode()).hexdigest()


def _retry_columns(conn):
    return sorted(r[0] for r in conn.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name='of_engine_model_call' "
        "AND column_name IN ('retry_not_before','failure_detail_json')")))


def _assert_complete(engine):
    with engine.begin() as c:
        check_schema(c)
        assert c.scalar(text("SELECT version_num FROM alembic_version")) == HEAD == "0138_tip_evidence_kind"
        assert _retry_columns(c) == ["failure_detail_json", "retry_not_before"]
        assert _guard_digest(c) == R0_GUARD_DIGEST
        assert {r[0] for r in c.execute(text("SELECT conname FROM pg_constraint WHERE conrelid='of_engine_model_call'::regclass"))} >= {
            "ck_of_engine_model_call_failure_detail", "ck_of_engine_model_call_retry_due"}


STARTS = [("original", "0129_reconnect_receipt"), ("original", "0130_commercial_vault_provenance"),
          ("original", "0131_manual_category_prices"), ("original", "0132_account_preparation"),
          ("original", "0133_preparation_retry_state"), ("current", "0129b_model_retry_due")]


def test_the_repair_follows_0136_and_the_head_is_0138_on_top_of_it():
    assert HEAD == "0138_tip_evidence_kind" and S.REVISION_RANK[REPAIR] == 137 and S.REVISION_RANK[HEAD] == 138
    from alembic.script import ScriptDirectory
    script = ScriptDirectory.from_config(Config(str(MIGRATIONS / "alembic.ini")))
    assert script.get_heads() == [HEAD]
    assert script.get_revision(REPAIR).down_revision == "0136_history_coverage"
    assert script.get_revision(HEAD).down_revision == REPAIR


@pytest.mark.parametrize("lineage,start", STARTS)
def test_supported_upgrade_entry_point_from_each_historical_state(blank, tmp_path, lineage, start):
    _build(blank, tmp_path, start, lineage=lineage)
    with blank.begin() as c:
        assert c.scalar(text("SELECT version_num FROM alembic_version")) == start
    _populate(blank)
    before = _snapshot(blank)
    with blank.begin() as c:
        S.upgrade(c)
    _assert_complete(blank)
    _assert_rows_preserved(before, _snapshot(blank))


@pytest.mark.parametrize("lineage,start", STARTS)
def test_plain_alembic_upgrade_head_is_also_complete(blank, tmp_path, lineage, start):
    _build(blank, tmp_path, start, lineage=lineage)
    _populate(blank)
    before = _snapshot(blank)
    with blank.begin() as c:
        command.upgrade(S._config(c), "head")
    _assert_complete(blank)
    _assert_rows_preserved(before, _snapshot(blank))


def test_database_raw_upgraded_to_0136_without_0129b_is_repaired_by_the_forward_revision(blank, tmp_path):
    _build(blank, tmp_path, "0133_preparation_retry_state", lineage="original")
    _populate(blank)
    before = _snapshot(blank)
    with blank.begin() as c:
        command.upgrade(S._config(c), "0136_history_coverage")  # skips 0129b: the stranded state
        assert c.scalar(text("SELECT version_num FROM alembic_version")) == "0136_history_coverage"
        assert _retry_columns(c) == []
        with pytest.raises(SchemaMismatch, match="retry_not_before"):
            check_schema(c, expected="0136_history_coverage")
    with blank.begin() as c:
        S.upgrade(c)
    _assert_complete(blank)
    _assert_rows_preserved(before, _snapshot(blank))


def test_a_tampered_guard_is_refused_and_nothing_changes(blank, tmp_path):
    _build(blank, tmp_path, "0133_preparation_retry_state", lineage="original")
    _populate(blank)
    with blank.begin() as c:
        src = c.scalar(text("SELECT p.prosrc FROM pg_proc p WHERE p.proname='of_engine_model_call_guard'"))
        c.execute(text("CREATE OR REPLACE FUNCTION of_engine_model_call_guard() RETURNS trigger LANGUAGE plpgsql AS $$BEGIN "
                       "RETURN NEW; END $$"))
    before = _snapshot(blank)
    with pytest.raises(SchemaMismatch):
        with blank.begin() as c:
            S.upgrade(c)
    with pytest.raises(Exception):  # the repair never replaces a guard body it does not recognise
        with blank.begin() as c:
            command.upgrade(S._config(c), "head")
    with blank.begin() as c:
        assert c.scalar(text("SELECT version_num FROM alembic_version")) == "0133_preparation_retry_state"
        assert _retry_columns(c) == []
    assert _snapshot(blank) == before
    assert src  # the original body was readable


def test_a_partial_old_lineage_shape_is_refused(blank, tmp_path):
    """Columns present but the old guard: not a state either lineage produced; the supported entry point refuses."""
    _build(blank, tmp_path, "0133_preparation_retry_state", lineage="original")
    with blank.begin() as c:
        c.execute(text("ALTER TABLE of_engine_model_call ADD COLUMN retry_not_before TIMESTAMP WITHOUT TIME ZONE"))
        c.execute(text("DROP TRIGGER of_engine_model_call_guard ON of_engine_model_call"))
    with pytest.raises(SchemaMismatch):
        with blank.begin() as c:
            S.upgrade(c)


def test_repair_is_a_no_op_on_a_current_database_and_its_downgrade_never_removes_0129b_ddl(blank, tmp_path):
    _build(blank, tmp_path, "0136_history_coverage", lineage="current")
    _populate(blank)
    before = _snapshot(blank)
    with blank.begin() as c:
        digest = _guard_digest(c)
        S.upgrade(c)
    _assert_complete(blank)
    with blank.begin() as c:
        assert _guard_digest(c) == digest == R0_GUARD_DIGEST
        command.downgrade(S._config(c), "0136_history_coverage")
    with blank.begin() as c:
        check_schema(c, expected="0136_history_coverage")
        assert _retry_columns(c) == ["failure_detail_json", "retry_not_before"]
    with blank.begin() as c:
        S.upgrade(c)
    _assert_complete(blank)
    _assert_rows_preserved(before, _snapshot(blank))
