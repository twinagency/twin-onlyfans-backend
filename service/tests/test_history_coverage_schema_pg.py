"""0136_history_coverage: additive schema, retained evidence, invariants the database itself enforces.

Real PostgreSQL. No provider, no model, no network.
"""
from datetime import timedelta
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_onlyfans_foundation_pg import database, seed
from db.schema import HEAD, check_schema, _config
from onlyfans_runtime.store import utcnow

TABLES = ("of_fans_import_source", "of_fans_history_cursor", "of_fans_import_gap", "of_fans_message_fact", "of_fans_read_meter")


def source_row(account, **over):
    now = utcnow()
    row = dict(account_id=account, source="transactions", state="running", revision=1, boundary_at=now, cursor_json="{}",
               requests_budget=10, credits_budget=20, min_interval_seconds=0, evidence_json="{}", created_at=now, updated_at=now)
    row.update(over)
    return row


def insert(engine, table, row):
    cols = ",".join(row)
    marks = ",".join(":" + k for k in row)
    with engine.begin() as c:
        c.execute(text(f"INSERT INTO {table} ({cols}) VALUES ({marks})"), row)


def refuses(engine, sql, params=None, match=None):
    with pytest.raises(DBAPIError) as caught:
        with engine.begin() as c:
            c.execute(text(sql), params or {})
    if match:
        assert match in str(caught.value)


def test_head_is_0136_and_every_table_exists_with_row_security(database):
    url, engine = database
    assert HEAD == "0136_history_coverage"
    with engine.connect() as c:
        check_schema(c)
        assert c.scalar(text("SELECT version_num FROM alembic_version")) == "0136_history_coverage"
        for table in TABLES:
            assert c.scalar(text("SELECT relrowsecurity FROM pg_class WHERE oid=to_regclass(:t)"), {"t": table}), table


def test_nothing_in_the_import_tables_can_be_deleted(database):
    url, engine = database
    store = seed(url, engine, account="acc-d")
    now = utcnow()
    try:
        insert(engine, "of_fans_import_source", source_row("acc-d"))
        insert(engine, "of_fans_history_cursor", dict(account_id="acc-d", fan_id=7, status="needed", priority=1, created_at=now, updated_at=now))
        insert(engine, "of_fans_import_gap", dict(account_id="acc-d", gap_id="g1", source="chats", kind="page", reason_code="provider_rate_limited", created_at=now, updated_at=now))
        insert(engine, "of_fans_message_fact", dict(account_id="acc-d", fan_id=7, message_id=9, price_cents=1500, media_ids_json="[1]",
                                                     via="history", raw_sha256="a" * 64, sent_at=now, created_at=now))
        insert(engine, "of_fans_read_meter", dict(account_id="acc-d", source="chats", hour=now.replace(minute=0, second=0, microsecond=0), updated_at=now))
        for table in TABLES:
            refuses(engine, f"DELETE FROM {table}", match="retained")
        refuses(engine, "DELETE FROM of_runtime WHERE account_id='acc-d'")
    finally:
        store.close()


def test_counter_budget_and_resolution_invariants_are_enforced_by_the_database(database):
    url, engine = database
    store = seed(url, engine, account="acc-i")
    now = utcnow()
    try:
        # seen must equal the sum of its outcome classes
        refuses(engine, "INSERT INTO of_fans_import_source (account_id,source,state,revision,boundary_at,cursor_json,rows_seen,requests_budget,credits_budget,min_interval_seconds,evidence_json,created_at,updated_at) "
                        "VALUES ('acc-i','chats','running',1,now(),'{}',3,1,1,0,'{}',now(),now())", match="reconcile")
        insert(engine, "of_fans_import_source", source_row("acc-i", rows_seen=3, rows_new=1, rows_duplicate=1, rows_quarantined=1))
        refuses(engine, "UPDATE of_fans_import_source SET requests_used=11", match="budget")
        refuses(engine, "UPDATE of_fans_import_source SET credits_reserved=21", match="budget")
        refuses(engine, "UPDATE of_fans_import_source SET rows_late=4", match="late")
        refuses(engine, "UPDATE of_fans_import_source SET state='done'", match="state")
        refuses(engine, "UPDATE of_fans_import_source SET source='vault'", match="source")
        # a cursor is complete exactly when it has a completion time
        refuses(engine, "INSERT INTO of_fans_history_cursor (account_id,fan_id,status,priority,created_at,updated_at) VALUES ('acc-i',1,'complete',0,now(),now())", match="completed")
        refuses(engine, "INSERT INTO of_fans_history_cursor (account_id,fan_id,status,priority,completed_at,created_at,updated_at) VALUES ('acc-i',2,'needed',0,now(),now(),now())", match="completed")
        # a gap is resolved by somebody or by nobody
        insert(engine, "of_fans_import_gap", dict(account_id="acc-i", gap_id="g", source="chats", kind="row", reason_code="row_refused", created_at=now, updated_at=now))
        refuses(engine, "UPDATE of_fans_import_gap SET resolved_at=now()", match="resolution")
        # the meter cannot claim more outcomes than requests
        refuses(engine, "INSERT INTO of_fans_read_meter (account_id,source,hour,requests,ok,updated_at) VALUES ('acc-i','chats',now(),1,2,now())", match="counters")
    finally:
        store.close()


def test_message_facts_are_frozen_except_for_a_later_observed_opening(database):
    url, engine = database
    store = seed(url, engine, account="acc-f")
    now = utcnow()
    try:
        insert(engine, "of_fans_message_fact", dict(account_id="acc-f", fan_id=5, message_id=50, price_cents=1500, media_ids_json="[1,2]",
                                                     preview_ids_json="[1]", via="history", raw_sha256="b" * 64, sent_at=now, created_at=now))
        for column, value in (("price_cents", "1600"), ("media_ids_json", "'[1]'"), ("preview_ids_json", "NULL"), ("locked_text", "true"),
                              ("via", "'webhook'"), ("sent_at", "now() + interval '1 day'")):
            refuses(engine, f"UPDATE of_fans_message_fact SET {column}={value}", match="immutable")
        with engine.begin() as c:   # not opened -> opened is the one allowed transition
            c.execute(text("UPDATE of_fans_message_fact SET is_opened=true, opened_at=now()"))
            c.execute(text("UPDATE of_fans_message_fact SET conflicted=true"))
        refuses(engine, "UPDATE of_fans_message_fact SET is_opened=false", match="immutable")
        refuses(engine, "UPDATE of_fans_message_fact SET is_opened=NULL", match="immutable")
        refuses(engine, "UPDATE of_fans_message_fact SET conflicted=false", match="immutable")
        refuses(engine, "INSERT INTO of_fans_message_fact (account_id,fan_id,message_id,price_cents,media_ids_json,via,raw_sha256,sent_at,created_at) "
                        "VALUES ('acc-f',5,51,0,'[1]','history','c',now(),now())", match="shape")
    finally:
        store.close()


def test_downgrade_refuses_to_destroy_evidence_and_is_otherwise_reversible(database):
    from alembic import command
    url, engine = database
    store = seed(url, engine, account="acc-g")
    try:
        insert(engine, "of_fans_import_source", source_row("acc-g"))
        with pytest.raises(RuntimeError, match="Preserve"):
            with engine.begin() as c:
                command.downgrade(_config(c), "0133_preparation_retry_state")
        with engine.connect() as c:
            check_schema(c)
            assert c.scalar(text("SELECT count(*) FROM of_fans_import_source")) == 1
    finally:
        store.close()


def test_empty_schema_downgrades_and_upgrades_again(database):
    from alembic import command
    url, engine = database
    with engine.begin() as c:
        command.downgrade(_config(c), "0133_preparation_retry_state")
    with engine.connect() as c:
        assert c.scalar(text("SELECT version_num FROM alembic_version")) == "0133_preparation_retry_state"
        assert c.scalar(text("SELECT to_regclass('of_fans_import_source')")) is None
        assert c.scalar(text("SELECT count(*) FROM pg_proc WHERE proname IN ('of_fans_history_retained','of_fans_message_fact_guard')")) == 0
    with engine.begin() as c:
        command.upgrade(_config(c), "0136_history_coverage")
    with engine.connect() as c:
        check_schema(c)


def test_removed_protection_is_detected_by_the_schema_check(database):
    url, engine = database
    with engine.begin() as c:
        c.execute(text("DROP TRIGGER of_fans_import_gap_retained ON of_fans_import_gap"))
    with engine.connect() as c:
        with pytest.raises(Exception, match="history"):
            check_schema(c)
