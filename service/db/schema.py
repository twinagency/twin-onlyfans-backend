"""Explicit PostgreSQL lifecycle. API/worker startup only inspects schema.

The historical chain starts from mutable ORM metadata and cannot be replayed
from empty. Bootstrap uses a frozen 0070 snapshot, then ordinary migrations.
Populated databases must identify a supported revision and pass the same
structural checks; an old self-healed SQLite schema is an import, not a stamp.
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Connection, make_url

BASELINE = "0070_onlyfans_read_bindings"
FOUNDATION = "0071_onlyfans_foundation"
DELIVERY = "0072_onlyfans_text_delivery"
EGRESS = "0073_one_use_egress"
NATIVE = "0074_native_text_plans"
BUDGET = "0075_native_model_budget"
MEMORY = "0076_native_memory_evidence"
MEDIA = "0077_native_media_sales"
CONTROLS = "0078_operator_controls"
CONFIGURATION = "0079_native_configuration"
CAMPAIGNS = "0080_onlyfans_campaigns"
CONNECTION = "0081_native_connection"
CONTENT = "0082_content_management"
VAULT = "0083_vault_management"
LEDGER = "0084_native_ledger"
CREATOR_FACTS = "0085_shared_creator_facts"
VAULT_EDITS = "0086_vault_edits"
ENROLLMENT = "0087_creator_enrollment"
VAULT_MEDIA = "0088_native_vault_media"
OFFER_IMPORT = "0089_offer_import"
LAUNCH = "0090_launch"
HEAD = "0091_launch_controls"
REVISION_RANK = {"0064_user_llm_keys": 64, BASELINE: 70, FOUNDATION: 71, DELIVERY: 72, EGRESS: 73, NATIVE: 74, BUDGET: 75, MEMORY: 76, MEDIA: 77, CONTROLS: 78, CONFIGURATION: 79, CAMPAIGNS:80, CONNECTION:81, CONTENT:82, VAULT:83, LEDGER:84, CREATOR_FACTS:85, VAULT_EDITS:86, ENROLLMENT:87, VAULT_MEDIA:88, OFFER_IMPORT:89, LAUNCH:90, HEAD:91}
HERE = Path(__file__).resolve().parent


class SchemaMismatch(RuntimeError):
    pass


def _metadata():
    from .models import Base
    from . import onlyfans_models  # noqa: F401 — register current tables
    from . import onlyfans_delivery_models  # noqa: F401 — register current tables
    from . import onlyfans_campaign_models,onlyfans_connection_models,onlyfans_content_models  # noqa: F401
    from . import onlyfans_vault_models, onlyfans_ledger_models, onlyfans_creator_facts_models, onlyfans_vault_edit_models  # noqa: F401
    from . import onlyfans_media_models  # noqa: F401
    from . import onlyfans_offer_import_models  # noqa: F401
    from . import onlyfans_launch_models  # noqa: F401
    from . import onlyfans_content_sync_progress_models  # noqa: F401
    from onlyfans_runtime import launch_operations_models  # noqa: F401
    from onlyfans_runtime.launch_ledger import models as _ledger_retry_models  # noqa: F401
    return Base.metadata


def _since_rank(item, *, fallback=0):
    since = item.info.get("onlyfans_since")
    if since is None:
        return fallback
    if since not in REVISION_RANK:
        raise SchemaMismatch(f"Unknown metadata revision {since}")
    return REVISION_RANK[since]


def _table_rank(table):
    if table.info.get("onlyfans_foundation"):
        fallback = REVISION_RANK[FOUNDATION]
    elif table.name in {"of_creator_bindings", "of_conversation_state", "of_message_delivery"}:
        fallback = REVISION_RANK[BASELINE]
    else:
        fallback = 0
    return _since_rank(table, fallback=fallback)


def revision(conn: Connection) -> str | None:
    if not inspect(conn).has_table("alembic_version"):
        return None
    versions = conn.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    if len(versions) != 1:
        raise SchemaMismatch("Expected one schema revision")
    return versions[0]


def check_schema(conn: Connection, *, expected: str = HEAD) -> None:
    """No DDL, repair, stamping or implicit compatibility fallback."""
    if conn.dialect.name != "postgresql":
        raise SchemaMismatch("OnlyFans runtime requires PostgreSQL")
    # Outside the dump's public schema: committed BEFORE pg_restore can write
    # any old authority records. A crash cannot leave a restored sender ready.
    if "onlyfans_restore" in inspect(conn).get_schema_names():
        raise SchemaMismatch("Restored database is quarantined pending authoritative reconciliation")
    if expected not in REVISION_RANK:
        raise SchemaMismatch("Unsupported expected schema revision")
    if revision(conn) != expected:
        raise SchemaMismatch("Database revision is not supported by this process")
    inspector = inspect(conn)
    tables = set(inspector.get_table_names())
    failures = []
    expected_rank = REVISION_RANK[expected]
    for table in _metadata().sorted_tables:
        if _table_rank(table) > expected_rank:
            continue
        if table.name not in tables:
            failures.append(f"missing table {table.name}")
            continue
        actual = {c["name"]: c for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if _since_rank(column) > expected_rank:
                continue
            other = actual.get(column.name)
            if other is None or other["type"]._type_affinity != column.type._type_affinity:
                failures.append(f"incompatible column {table.name}.{column.name}")
            elif not column.nullable and other["nullable"]:
                failures.append(f"nullable required column {table.name}.{column.name}")
        pk = inspector.get_pk_constraint(table.name)["constrained_columns"]
        if pk != [c.name for c in table.primary_key]:
            failures.append(f"incompatible key {table.name}")
        indexes = {i["name"]: i for i in inspector.get_indexes(table.name)}
        for index in table.indexes:
            if _since_rank(index) > expected_rank or any(_since_rank(c) > expected_rank for c in index.columns):
                continue
            other = indexes.get(index.name)
            if other is None or other["unique"] != bool(index.unique) or other["column_names"] != [c.name for c in index.columns]:
                failures.append(f"incompatible index {index.name}")
            elif index.dialect_options["postgresql"].get("where") is not None:
                # A full unique constraint is NOT an equivalent active-binding index.
                predicate = str(other.get("dialect_options", {}).get("postgresql_where", ""))
                if not predicate or (index.name == "uq_of_creator_bindings_active_account" and predicate.replace("(", "").replace(")", "").strip() != "revoked_at IS NULL"):
                    failures.append(f"incompatible predicate {index.name}")
        actual_fks = {(tuple(f["constrained_columns"]), f["referred_table"], tuple(f["referred_columns"])) for f in inspector.get_foreign_keys(table.name)}
        for fk in table.foreign_key_constraints:
            if any(_since_rank(c) > expected_rank for c in fk.columns):
                continue
            signature = (tuple(c.name for c in fk.columns), fk.referred_table.name, tuple(e.column.name for e in fk.elements))
            if signature not in actual_fks:
                failures.append(f"missing foreign key {table.name}")
        actual_uniques = {tuple(c["column_names"]) for c in inspector.get_unique_constraints(table.name)}
        for constraint in table.constraints:
            if constraint.__class__.__name__ == "UniqueConstraint" and _since_rank(constraint) <= expected_rank and all(_since_rank(c) <= expected_rank for c in constraint.columns):
                if tuple(c.name for c in constraint.columns) not in actual_uniques:
                    failures.append(f"missing unique constraint {table.name}")
        if table.name == "of_content_sync_conflict" and 89 <= expected_rank < 91:
            if ("account_id", "item_id", "observation_id") not in actual_uniques:
                failures.append("missing historical unique constraint of_content_sync_conflict")
        expected_checks = {c.name for c in table.constraints if c.__class__.__name__ == "CheckConstraint" and c.name and _since_rank(c) <= expected_rank}
        actual_checks = {c["name"] for c in inspector.get_check_constraints(table.name)}
        if not expected_checks.issubset(actual_checks):
            failures.append(f"missing check constraint {table.name}")
    for rank, table, trigger, message in [
        (76, "of_native_memory_change", "of_native_memory_immutable", "native memory evidence is append-only"),
        (77, "of_commerce_evidence", "of_commerce_immutable", "commerce evidence is append-only"),
        (78, "of_control_audit", "of_control_immutable", "control evidence is append-only"),
        (79, "of_configuration_change", "of_configuration_immutable", "configuration evidence is append-only"),
        (80, "of_campaign_change", "of_campaign_immutable", "campaign evidence is append-only"),
        (81, "of_connection_change", "of_connection_immutable", "connection evidence is append-only"),
        (82, "of_content_change", "of_content_immutable", "content evidence is append-only"),
        (83, "of_vault_product_create", "of_vault_product_immutable", "vault product evidence is append-only"),
        (86, "of_vault_edit", "of_vault_edit_immutable", "vault edit evidence is append-only"),
        (84, "of_ledger_evidence", "of_ledger_immutable", "ledger evidence is append-only"),
        (89, "of_offer_observation", "of_offer_observation_immutable", "offer observation evidence is append-only"),
        (89, "of_offer_import_decision", "of_offer_decision_immutable", "offer import decisions are append-only"),
        (89, "of_content_sync_decision", "of_content_sync_decision_immutable", "content sync decisions are append-only"),
        (90, "of_history_memory_evidence", "of_history_memory_evidence_immutable", "historical memory evidence is append-only"),
        (90, "of_history_memory_correction", "of_history_memory_correction_immutable", "historical memory corrections are append-only"),
        (90, "of_personality_revision", "of_personality_immutable", "personality evidence is append-only"),
        (90, "of_engagement_settings_change", "of_engagement_settings_immutable", "engagement settings evidence is append-only"),
        (91, "of_launch_command", "of_launch_command_immutable", "launch command evidence is append-only"),
        (91, "of_ledger_resolution", "of_ledger_resolution_immutable", "ledger resolution evidence is append-only"),
        (91, "of_content_sync_occurrence", "of_content_sync_occurrence_immutable", "content source occurrence evidence is append-only"),
        (91, "of_ledger_retry_command", "of_ledger_retry_command_immutable", "ledger retry command evidence is append-only"),
    ]:
        if expected_rank < rank:
            continue
        rows = conn.execute(text("""SELECT t.tgenabled::text, t.tgtype::integer, t.tgqual, p.proname::text, p.prosrc::text, l.lanname::text
            FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_language l ON l.oid=p.prolang
            WHERE t.tgrelid=to_regclass(:table) AND t.tgname=:trigger AND NOT t.tgisinternal"""),
            {"table": table, "trigger": trigger}).all()
        if (len(rows) != 1 or rows[0][0] not in {"O", "A"} or rows[0][1] != 27 or rows[0][2] is not None
                or rows[0][3] != trigger or rows[0][5] != "plpgsql"
                or " ".join(rows[0][4].split()) != f"BEGIN RAISE EXCEPTION '{message}'; END;"):
            failures.append(f"missing append-only protection {table}")
    if expected_rank >= 88:
        guard = conn.execute(text("""SELECT t.tgenabled::text,t.tgtype::integer,p.proname::text,p.prosrc::text
            FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid
            WHERE t.tgrelid=to_regclass('of_vault_media_job') AND t.tgname='of_vault_media_guard'
            AND NOT t.tgisinternal""")).all()
        if (len(guard) != 1 or guard[0][0] not in {"O", "A"} or guard[0][1] != 27
                or guard[0][2] != "of_vault_media_guard"
                or hashlib.sha256(" ".join(guard[0][3].split()).encode()).hexdigest() != "95986d179ea6a7826b1fa0c8b84407efb8dcd7897a2dcc70bed69938a88ff3b6"):
            failures.append("missing media admission protection")
    if expected_rank >= 89:
        # Frozen-identity guards: job inputs, confirmed provider mappings, resolved conflicts.
        for table, trigger, digest in [
            ("of_offer_import_job", "of_offer_import_job_frozen", "a5ad24a783c3e0320b384e8493840e48eac853fb1f7f68264b86afe7f0abb755"),
            ("of_content_sync_asset", "of_content_sync_asset_frozen", "42da1a4289a67c7959f80e2362dbc058a4f8b21588970c5097e6a618f0cf0a36"),
            ("of_content_sync_conflict", "of_content_sync_conflict_frozen", ("7cbb8f8248aa39ddd6f97b82acfc35b49afde2883f89354f951f86a8fece5e97" if expected_rank >=91 else "3f46cf1266e826e98203ad807c53abcef7d9512ab26658915102a02acf450538")),
        ]:
            guard = conn.execute(text("""SELECT t.tgenabled::text,t.tgtype::integer,p.proname::text,p.prosrc::text
                FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid
                WHERE t.tgrelid=to_regclass(:table) AND t.tgname=:trigger AND NOT t.tgisinternal"""),
                {"table": table, "trigger": trigger}).all()
            if (len(guard) != 1 or guard[0][0] not in {"O", "A"} or guard[0][1] != 27 or guard[0][2] != trigger
                    or hashlib.sha256(" ".join(guard[0][3].split()).encode()).hexdigest() != digest):
                failures.append(f"missing frozen-identity protection {table}")
    if expected_rank >= 90:
        # Engagement actions: frozen definition, retained intent link/cancellation, terminal states stay terminal.
        guard = conn.execute(text("""SELECT t.tgenabled::text,t.tgtype::integer,p.proname::text,p.prosrc::text
            FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid
            WHERE t.tgrelid=to_regclass('of_engagement_action') AND t.tgname='of_engagement_action_guard'
            AND NOT t.tgisinternal""")).all()
        if (len(guard) != 1 or guard[0][0] not in {"O", "A"} or guard[0][1] != 27
                or guard[0][2] != "of_engagement_action_guard"
                or hashlib.sha256(" ".join(guard[0][3].split()).encode()).hexdigest() != "fec38663e2d7b442e4f29bd56d1f22d4a521bda9eb1a80f4eaeb1e97b9693850"):
            failures.append("missing frozen-identity protection of_engagement_action")
    if "per_employee_revenue_with_attribution" not in inspector.get_view_names():
        failures.append("missing revenue view")
    if failures:
        raise SchemaMismatch("; ".join(failures))


def _config(conn: Connection) -> Config:
    cfg = Config(str(HERE / "migrations/alembic.ini"))
    cfg.attributes["connection"] = conn
    return cfg


def bootstrap(conn: Connection) -> None:
    """Caller owns transaction; never stamp or overlay any existing schema."""
    if conn.dialect.name != "postgresql":
        raise SchemaMismatch("Bootstrap requires PostgreSQL")
    conn.execute(text("SELECT pg_advisory_xact_lock(7811070)"))
    if inspect(conn).get_table_names() or inspect(conn).get_view_names():
        raise SchemaMismatch("Bootstrap requires an empty database schema")
    conn.exec_driver_sql((HERE / "snapshots/0070_postgresql.sql").read_text())
    command.stamp(_config(conn), BASELINE)
    check_schema(conn, expected=BASELINE)
    command.upgrade(_config(conn), HEAD)
    check_schema(conn)


def upgrade(conn: Connection) -> None:
    if conn.dialect.name != "postgresql":
        raise SchemaMismatch("Upgrade requires PostgreSQL")
    conn.execute(text("SELECT pg_advisory_xact_lock(7811070)"))
    current = revision(conn)
    if current not in REVISION_RANK:
        raise SchemaMismatch("Unsupported legacy schema; rehearse a validated import first")
    check_schema(conn, expected=current)
    command.upgrade(_config(conn), HEAD)
    check_schema(conn)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("bootstrap", "upgrade", "check"))
    parser.add_argument("--url-env", default="ONLYFANS_MIGRATION_URL", help="Environment variable containing the explicit target URL")
    args = parser.parse_args()
    import os
    raw = os.environ.get(args.url_env)
    if not raw or make_url(raw).get_backend_name() != "postgresql":
        parser.error("An explicit PostgreSQL target is required")
    url = make_url(raw).set(drivername="postgresql+psycopg2")
    engine = create_engine(url, pool_size=1, max_overflow=0, connect_args={"options": "-c timezone=UTC -c lock_timeout=5000 -c statement_timeout=30000"})
    try:
        with engine.begin() as conn:
            {"bootstrap": bootstrap, "upgrade": upgrade, "check": check_schema}[args.action](conn)
        print(f"OnlyFans schema {args.action}: ready ({HEAD})")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
