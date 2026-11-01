"""Rehearse the actual default-off branch, backup, and forward merge in isolation."""
from pathlib import Path
import json

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, seed
from test_onlyfans_recovery_pg import KEY, empty_database, pg_bin, rows
from test_onlyfans_release_package_pg import populated_preupgrade_database, proxy_rows
from test_fastt_controls_pg import STAFF
from db.models import Fan, OfConversationState
from db.schema import (CONVERSATION_DEFAULT_OFF, LEDGER_HISTORY, VAULT_CONTINUATION,
                       HEAD, SchemaMismatch, _config, check_schema, upgrade)
from onlyfans_runtime import operations, fastt_scope
from native_pilot.service import fan_hold_reason
from twin_bridge import store as bridge_store


@pytest.mark.parametrize("source", [LEDGER_HISTORY, CONVERSATION_DEFAULT_OFF, VAULT_CONTINUATION])
def test_genuine_source_merges_forward_preserving_choices_and_unknown(source):
    with populated_preupgrade_database() as (url, engine):
        with engine.begin() as c:
            command.upgrade(_config(c), source)
            check_schema(c, expected=source)
        before = rows(engine)
        with engine.begin() as c:
            for table in ("messages", "of_content_sync_source", "of_runtime",
                          "of_content_sync_account", "of_offer_import_job"):
                c.exec_driver_sql("ANALYZE public." + table)
        with engine.begin() as c:
            c.exec_driver_sql("SET LOCAL lock_timeout='5s'")
            c.exec_driver_sql("SET LOCAL statement_timeout='30s'")
            upgrade(c)
            upgrade(c)
            check_schema(c)
            assert c.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
            assert "'paused'" in c.scalar(text(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='of_conversation_state' AND column_name='mode'"))
        after = rows(engine)
        for name in ("accounts", "fans", "messages", "of_conversation_state", "of_message_delivery"):
            # Additive migrations introduce nullable columns. Compare every
            # original field and every row, rather than treating those added
            # keys as a destructive data change. HEAD shape is checked above.
            original, upgraded = before[name] or [], after[name] or []
            assert len(original) == len(upgraded), name
            if original:
                columns = set(original[0])
                assert all(columns <= set(row) for row in upgraded), name
                projected = [{key: row[key] for key in columns} for row in upgraded]
                assert sorted(json.dumps(row, sort_keys=True) for row in original) == sorted(
                    json.dumps(row, sort_keys=True) for row in projected), name
        assert all(row["of_profile_checked_at"] is None and row["of_profile_error"] is None
                   for row in after["fans"])
        assert after["of_conversation_state"][0]["unresolved_attempt_id"] == "unknown-preupgrade"


def test_real_default_off_backup_includes_native_receipts_and_restores_quarantined(tmp_path, pg_bin):
    with populated_preupgrade_database() as (url, engine):
        with engine.begin() as c:
            command.upgrade(_config(c), CONVERSATION_DEFAULT_OFF)
            check_schema(c, expected=CONVERSATION_DEFAULT_OFF)
        bridge_store.ensure_runtime_schema(url)
        with engine.begin() as c:
            c.exec_driver_sql("""INSERT INTO fastt_runtime.twin_bridge_send_attempts
                (id,account_id,fan_id,turn_id,attempt_no,state,owner_token,revision,created_at)
                VALUES (777,'preupgrade',1,'retained',1,'delivery_unknown','retained-owner',4,CURRENT_TIMESTAMP)""")
            c.exec_driver_sql("""INSERT INTO fastt_runtime.native_pilot_receipts
                (attempt_id,pilot_id,input_key,bubble_no,state,created_at,updated_at)
                VALUES (777,'retained','retained',1,'delivery_unknown','2026-10-27 00:00:00.000000','2026-10-27 00:00:00.000000')""")
        before, proxies = rows(engine), proxy_rows(engine)
        archive = tmp_path / "actual-source.twinof"
        meta = operations.backup(url, archive, key=KEY, pg_bin=pg_bin,
                                include_proxy=True, source_revision=CONVERSATION_DEFAULT_OFF)
        assert meta["scope"] == "public+onlyfans_proxy+fastt_runtime"
        with empty_database() as (target, restored):
            result = operations.restore(target, archive, key=KEY, pg_bin=pg_bin)
            assert result["quarantined"] and not result["releasePermitted"]
            actual = rows(restored)
            expected = dict(before)
            actual.pop("alembic_version"); expected.pop("alembic_version")
            assert actual == expected and proxy_rows(restored) == proxies
            with restored.connect() as c:
                assert c.scalar(text("SELECT state FROM fastt_runtime.native_pilot_receipts WHERE attempt_id=777")) == "delivery_unknown"
                assert c.scalar(text("SELECT state FROM fastt_runtime.twin_bridge_send_attempts WHERE id=777")) == "delivery_unknown"
                with pytest.raises(SchemaMismatch, match="quarantined"):
                    check_schema(c, expected=CONVERSATION_DEFAULT_OFF)
            from native_pilot.ledger import Ledger, Refused
            restored_ledger = Ledger(target, allowance=True)
            with pytest.raises(Refused, match="restore-quarantined"):
                with restored_ledger._tx():
                    raise AssertionError("restored sender admitted a transaction")
        assert rows(engine) == before and proxy_rows(engine) == proxies
        bridge_store.dispose_pools()


def test_default_off_backup_rejects_false_source_and_partial_upgrade(tmp_path, pg_bin):
    with populated_preupgrade_database() as (url, engine):
        with pytest.raises(SchemaMismatch):
            operations.backup(url, tmp_path / "wrong.twinof", key=KEY, pg_bin=pg_bin,
                              source_revision=CONVERSATION_DEFAULT_OFF)
        with engine.begin() as c:
            command.upgrade(_config(c), CONVERSATION_DEFAULT_OFF)
            c.exec_driver_sql("ALTER TABLE of_conversation_state ALTER COLUMN mode SET DEFAULT 'automated'")
        with pytest.raises(SchemaMismatch, match="default must be paused"):
            operations.backup(url, tmp_path / "unsafe.twinof", key=KEY, pg_bin=pg_bin,
                              source_revision=CONVERSATION_DEFAULT_OFF)
        with engine.begin() as c:
            c.exec_driver_sql("ALTER TABLE of_conversation_state ALTER COLUMN mode SET DEFAULT 'paused'")
            c.exec_driver_sql("ALTER TABLE content_offers ADD COLUMN post_buy_started_at timestamp")
        with pytest.raises(operations.RecoveryRefused, match="structure differs"):
            operations.backup(url, tmp_path / "partial.twinof", key=KEY, pg_bin=pg_bin,
                              source_revision=CONVERSATION_DEFAULT_OFF)
        assert not list(tmp_path.glob("*.twinof"))


def test_defaults_and_button_controls_remain_account_scoped(database, monkeypatch):
    url, engine = database
    app = seed(url, engine)
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("ONLYFANS_FASTT_SCOPE", "db:*")
    fastt_scope.reset_cache()
    try:
        with Session(engine) as s, s.begin():
            s.add_all([Fan(account_id="acc-a", fan_id=201), Fan(account_id="acc-a", fan_id=202)])
            s.flush()
            s.add(OfConversationState(account_id="acc-a", fan_id=201))
            s.flush()
            s.execute(text("INSERT INTO of_conversation_state(account_id,fan_id) VALUES ('acc-a',202)"))
        with Session(engine) as s:
            assert s.get(OfConversationState, ("acc-a", 201)).mode == "paused"
            assert s.get(OfConversationState, ("acc-a", 202)).mode == "paused"
        from onlyfans_runtime.fastt_manual import fastt_serves
        assert fastt_serves("acc-a", 201)
        assert fan_hold_reason(url, "acc-a", 201, strict=True) == "fan-paused"
        for revision, action, mode, held in (
            (0, "resume_automation", "automated", None),
            (1, "request_takeover", "human_controlled", "fan-restricted"),
            (2, "release_takeover", "automated", None),
            (3, "pause_automation", "paused", "fan-restricted"),
        ):
            assert app.control("tenant-a", "acc-a", 201, mode=mode, action=action,
                               expected_revision=revision, command_key="compat-" + action,
                               actor=STAFF, reason="Synthetic control test") == revision + 1
            assert fan_hold_reason(url, "acc-a", 201, strict=True) == held
        with Session(engine) as s:
            assert s.get(OfConversationState, ("acc-a", 202)).mode == "paused"
            assert s.get(OfConversationState, ("acc-a", 101)).mode == "automated"
    finally:
        app.close()
        fastt_scope.reset_cache()
        bridge_store.dispose_pools()
