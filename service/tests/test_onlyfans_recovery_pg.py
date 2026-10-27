"""Actual encrypted pg_dump/pg_restore, with synthetic PostgreSQL evidence only."""
import base64
from contextlib import contextmanager
import multiprocessing
import os
from pathlib import Path
import shutil
import time
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from test_onlyfans_text_delivery_pg import configured, prepare, ACCOUNT, TENANT
from db.models import Message
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.schema import SchemaMismatch, check_schema
from onlyfans_runtime import operations as ops
from onlyfans_runtime.store import Store, utcnow
from onlyfans_runtime.text_delivery import SyntheticTextTransport

KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()


@pytest.fixture
def pg_bin():
    value = os.environ.get("ONLYFANS_TEST_PG_BIN")
    if value:
        assert (Path(value) / ("pg_dump.exe" if os.name == "nt" else "pg_dump")).is_file() and (Path(value) / ("pg_restore.exe" if os.name == "nt" else "pg_restore")).is_file()
        return value
    assert shutil.which("pg_dump") and shutil.which("pg_restore"), "Install matching PostgreSQL clients or set ONLYFANS_TEST_PG_BIN"
    return None


@contextmanager
def empty_database():
    admin = create_engine(os.environ["ONLYFANS_TEST_PG_URL"], isolation_level="AUTOCOMMIT")
    name = "of_restore_" + uuid.uuid4().hex
    with admin.connect() as c:
        c.exec_driver_sql(f'CREATE DATABASE "{name}" TEMPLATE template0 ENCODING \'UTF8\'')
    url = admin.url.set(database=name).render_as_string(hide_password=False)
    engine = create_engine(url)
    try:
        yield url, engine
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
        admin.dispose()


def rows(engine):
    with engine.connect() as c:
        return {name: c.scalar(text(f'SELECT jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text) FROM "{name}" t'))
                for name in inspect(c).get_table_names(schema="public")}


def accepted_fixture(database):
    store, authority = configured(database)
    intent = prepare(authority, body="PRIVATE SYNTHETIC BACKUP TEXT 🦋")
    assert authority.dispatch(TENANT, ACCOUNT, intent, SyntheticTextTransport(ACCOUNT)) == "accepted"
    return store, authority


def test_encrypted_roundtrip_retains_all_public_evidence_and_blocks_startup(database, tmp_path, pg_bin):
    url, engine = database
    store, authority = accepted_fixture(database)
    archive = tmp_path / "snapshot.twinof"
    before = rows(engine)
    metadata = ops.backup(url, archive, key=KEY, pg_bin=pg_bin)
    assert archive.stat().st_mode & 0o777 == 0o600
    encrypted = archive.read_bytes()
    assert b"PRIVATE SYNTHETIC BACKUP TEXT" not in encrypted
    assert b"PGDMP" not in encrypted and KEY.encode() not in encrypted
    # A newer send exists after this backup. Restoring it must never erase the
    # source ledger, refund its cap or permit reissuing old work in the target.
    with Session(engine) as s, s.begin():
        s.get(OfDeliveryPolicy, ACCOUNT).next_dispatch_at = utcnow()
    later = prepare(authority, key="after-backup", body="Newer unknown attempt")
    assert authority.dispatch(TENANT, ACCOUNT, later, SyntheticTextTransport(ACCOUNT, outcome="timeout")) == "unknown"
    with empty_database() as (target, restored):
        result = ops.restore(target, archive, key=KEY, pg_bin=pg_bin)
        assert result["backupId"] == metadata["backupId"]
        assert result["quarantined"] and result["releasePermitted"] is False
        after = rows(restored)
        assert after.pop("alembic_version") == [{"version_num": ops.QUARANTINE_REVISION}]
        expected = dict(before); expected.pop("alembic_version")
        assert after == expected, "restore must preserve all history/caps/receipts/control verbatim"
        with restored.connect() as c:
            assert c.scalar(text("SELECT status FROM onlyfans_restore.fence")) == "quarantined"
            with pytest.raises(SchemaMismatch, match="quarantined"):
                check_schema(c)
        with pytest.raises(SchemaMismatch, match="quarantined"):
            Store(target)
    with Session(engine) as s:
        assert s.get(OfTextIntent, (ACCOUNT, later)).status == "unknown"
        assert s.get(OfDeliveryPolicy, ACCOUNT).used_units == 2
        assert s.query(Message).filter_by(direction="out").count() == 1
    store.close()


@pytest.mark.parametrize("damage", ["wrong_key", "tag", "truncated", "header", "oversized"])
def test_untrusted_archive_is_refused_before_any_target_mutation(database, tmp_path, pg_bin, damage):
    url, _ = database
    store, _ = accepted_fixture(database)
    archive = tmp_path / "snapshot.twinof"
    ops.backup(url, archive, key=KEY, pg_bin=pg_bin)
    raw = bytearray(archive.read_bytes())
    key = KEY
    if damage == "wrong_key": key = base64.urlsafe_b64encode(b"x" * 32).decode()
    elif damage == "tag": raw[-1] ^= 1
    elif damage == "truncated": raw = raw[:60]
    elif damage == "header": raw[0] ^= 1
    else: raw[len(ops.MAGIC):len(ops.MAGIC)+4] = b"\xff" * 4
    archive.write_bytes(raw)
    with empty_database() as (target, restored):
        with pytest.raises(ops.RecoveryRefused):
            ops.restore(target, archive, key=key, pg_bin=pg_bin)
        with restored.connect() as c:
            assert inspect(c).get_table_names() == []
            assert "onlyfans_restore" not in inspect(c).get_schema_names()
    store.close()


def test_populated_restore_target_and_existing_destination_cannot_be_overwritten(database, tmp_path, pg_bin):
    url, engine = database
    store, _ = accepted_fixture(database)
    archive = tmp_path / "snapshot.twinof"
    ops.backup(url, archive, key=KEY, pg_bin=pg_bin)
    old_bytes = archive.read_bytes()
    before = rows(engine)
    with pytest.raises(ops.RecoveryRefused, match="already exists"):
        ops.backup(url, archive, key=KEY, pg_bin=pg_bin)
    assert archive.read_bytes() == old_bytes
    with pytest.raises(ops.RecoveryRefused, match="empty"):
        ops.restore(url, archive, key=KEY, pg_bin=pg_bin)
    assert rows(engine) == before
    store.close()


def crash_after_fence(target, archive, pg_bin, pipe):
    @contextmanager
    def blocked(*a, **kw):
        pipe.send("fence_committed")
        time.sleep(30)
        yield
    ops._client = blocked
    ops.restore(target, archive, key=KEY, pg_bin=pg_bin)


def crash_after_archive_commit(target, archive, pg_bin):
    original = ops._client
    @contextmanager
    def stop_before_sealing(*a, **kw):
        with original(*a, **kw) as process:
            yield process
        os._exit(77)
    ops._client = stop_before_sealing
    ops.restore(target, archive, key=KEY, pg_bin=pg_bin)


def test_crash_after_archive_commit_leaves_no_ready_revision_for_older_binary(database, tmp_path, pg_bin):
    url, _ = database
    store, _ = accepted_fixture(database)
    archive = tmp_path / "snapshot.twinof"
    ops.backup(url, archive, key=KEY, pg_bin=pg_bin)
    with empty_database() as (target, restored):
        ctx = multiprocessing.get_context("spawn")
        process = ctx.Process(target=crash_after_archive_commit, args=(target, archive, pg_bin))
        process.start(); process.join(15)
        try:
            assert process.exitcode == 77
            with restored.connect() as c:
                assert c.scalar(text("SELECT count(*) FROM messages")) == 1
                assert c.scalar(text("SELECT count(*) FROM alembic_version")) == 0
                # The older supported check requires exactly one current
                # revision. It refuses without knowing the new fence schema.
                from db.schema import revision
                with pytest.raises(SchemaMismatch, match="one schema revision"):
                    revision(c)
                assert c.scalar(text("SELECT status FROM onlyfans_restore.fence")) == "in_progress"
        finally:
            if process.is_alive(): process.kill(); process.join(5)
    store.close()


def test_process_killed_after_fence_cannot_leave_old_sender_ready(database, tmp_path, pg_bin):
    url, _ = database
    store, _ = accepted_fixture(database)
    archive = tmp_path / "snapshot.twinof"
    ops.backup(url, archive, key=KEY, pg_bin=pg_bin)
    with empty_database() as (target, restored):
        ctx = multiprocessing.get_context("spawn")
        parent, child = ctx.Pipe()
        process = ctx.Process(target=crash_after_fence, args=(target, archive, pg_bin, child))
        process.start()
        try:
            assert parent.poll(10) and parent.recv() == "fence_committed"
            process.kill()
            process.join(5)
            assert process.exitcode != 0
            with restored.connect() as c:
                assert c.scalar(text("SELECT status FROM onlyfans_restore.fence")) == "in_progress"
                with pytest.raises(SchemaMismatch, match="quarantined"):
                    check_schema(c)
            with pytest.raises(SchemaMismatch, match="quarantined"):
                Store(target)
        finally:
            if process.is_alive(): process.kill(); process.join(5)
            parent.close(); child.close()
    store.close()


def test_archive_size_failure_leaves_no_partial_or_plaintext_file(database, tmp_path, pg_bin, monkeypatch):
    url, _ = database
    store, _ = accepted_fixture(database)
    monkeypatch.setattr(ops, "MAX_ARCHIVE_BYTES", 1)
    with pytest.raises(ops.RecoveryRefused, match="bounded"):
        ops.backup(url, tmp_path / "snapshot.twinof", key=KEY, pg_bin=pg_bin)
    assert list(tmp_path.iterdir()) == []
    store.close()


def test_client_environment_excludes_inherited_credentials(monkeypatch):
    for name in ["ONLYFANS_API_TOKEN", "ONLYFANS_BACKUP_KEY", "PGPASSWORD", "PGPASSFILE", "PGOPTIONS", "HTTP_PROXY"]:
        monkeypatch.setenv(name, "do-not-forward")
    env = ops._pg_env("postgresql://synthetic:declared-secret@127.0.0.1:5432/synthetic")
    assert "do-not-forward" not in env.values()
    assert env["PGPASSWORD"] == "declared-secret" and env["PGPASSFILE"] == os.devnull
    assert "PGOPTIONS" not in env and "ONLYFANS_API_TOKEN" not in env


@pytest.mark.parametrize("url", ["sqlite:///state.db", "postgresql:///missing-host", "postgresql://u@localhost/database?options=unsafe", "postgresql://u@localhost/postgresql%3A%2F%2Fevil"])
def test_ambiguous_client_connection_settings_are_refused(url):
    with pytest.raises(ops.RecoveryRefused):
        ops._pg_env(url)
