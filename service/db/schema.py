"""Explicit PostgreSQL lifecycle. API/worker startup only inspects schema.

The historical chain starts from mutable ORM metadata and cannot be replayed
from empty. Bootstrap uses a frozen 0070 snapshot, then ordinary migrations.
Populated databases must identify a supported revision and pass the same
structural checks; an old self-healed SQLite schema is an import, not a stamp.
"""
from __future__ import annotations

import argparse
import hashlib
import re
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
LAUNCH_CONTROLS = "0091_launch_controls"
LEDGER_HISTORY = "0092_ledger_history"
LEDGER_MARKERS = "0093_ledger_markers"
PURCHASE_REPLY = "0094_purchase_reply"
CONTENT_FOLD_PENDING = "0095_content_fold_pending"
VAULT_CONTINUATION = "0096_vault_continuation"
CONVERSATION_DEFAULT_OFF = "0093_conversation_default_off"
RELEASE_CONTROLS = "0097_release_controls"
LEDGER_EXCLUSION = "0098_ledger_exclusion"
FAN_IDENTITY_SYNC = "0099_fan_identity_sync"
MANUAL_MEDIA = "0100_manual_free_media"
CONTEXTUAL_OFFERS = "0101_contextual_offers"
DAILY_SEND_CAP = "0102_daily_send_cap"
CONCURRENT_DELIVERY = "0103_concurrent_delivery"
CONTEXTUAL_ITEM_SCOPE = "0104_contextual_item_scope"
OPERATOR_CASES = "0105_operator_cases"
READ_LANE_FAIRNESS = "0106_read_lane_fairness"
ACTIVE_SELLING_QUOTE = "0107_active_selling_quote"
COMMERCIAL_PROGRESS = "0108_commercial_progress"
EARNINGS_TOTALS = "0105_earnings_totals"
PARALLEL_HISTORY = "0106_parallel_history_reads"
HISTORY_RELEASE_MERGE = "0109_history_release_merge"
RECENT_HISTORY = "0110_recent_history"
MOXY = '0111_moxy_owner'
VAULT_WINDOW = '0112_vault_recent_window'
ENGINE = '0113_conversation_engine'
INDEPENDENCE = '0114_independent_replies'
ENGINE_MEMORY = '0115_engine_memory'
ENGINE_CATALOGUE = '0116_engine_catalogue'
ENGINE_PARTS = '0117_engine_message_parts'
ENGINE_OPERATOR = '0118_engine_operator'
ENGINE_IDENTITY = '0119_identity_independence'
FANS_INTAKE = '0120_fans_intake'
ENGINE_GENERATIONS = '0121_engine_generations'
FANSAPI_TRANSPORT = '0122_fansapi_transport'
ENGINE_MODEL_CALLS = '0123_engine_model_calls'
ENGINE_APPROVAL = '0124_engine_approval'
PLAN_C_SCOPE = '0125_plan_c_scope'
PLAN_C_INGRESS = '0126_plan_c_ingress'
OFFER_GRANT_LINEAGE = '0127_offer_grant_lineage'
MODEL_BILLING_EVIDENCE = '0128_model_billing_evidence'
RECONNECT_RECEIPT = '0129_reconnect_receipt'
MODEL_RETRY_DUE = '0129b_model_retry_due'  # R0, shared with the 0129 trial line; 0130 builds on it
COMMERCIAL_VAULT = "0130_commercial_vault_provenance"
CATEGORY_PRICES = "0131_manual_category_prices"
ACCOUNT_PREPARATION = "0132_account_preparation"
PREPARATION_RETRY_STATE = "0133_preparation_retry_state"
HEAD = PREPARATION_RETRY_STATE
REVISION_RANK = {"0064_user_llm_keys": 64, BASELINE: 70, FOUNDATION: 71, DELIVERY: 72, EGRESS: 73, NATIVE: 74, BUDGET: 75, MEMORY: 76, MEDIA: 77, CONTROLS: 78, CONFIGURATION: 79, CAMPAIGNS:80, CONNECTION:81, CONTENT:82, VAULT:83, LEDGER:84, CREATOR_FACTS:85, VAULT_EDITS:86, ENROLLMENT:87, VAULT_MEDIA:88, OFFER_IMPORT:89, LAUNCH:90, LAUNCH_CONTROLS:91, LEDGER_HISTORY:92, LEDGER_MARKERS:93, PURCHASE_REPLY:94, CONTENT_FOLD_PENDING:95, VAULT_CONTINUATION:96, CONVERSATION_DEFAULT_OFF:92, RELEASE_CONTROLS:97, LEDGER_EXCLUSION:98}
HERE = Path(__file__).resolve().parent
REVISION_RANK[FAN_IDENTITY_SYNC] = 99
REVISION_RANK[MANUAL_MEDIA] = 100
REVISION_RANK[CONTEXTUAL_OFFERS] = 101
REVISION_RANK[DAILY_SEND_CAP] = 102
REVISION_RANK[CONCURRENT_DELIVERY] = 103
REVISION_RANK[CONTEXTUAL_ITEM_SCOPE] = 104
REVISION_RANK[OPERATOR_CASES] = 105
REVISION_RANK[READ_LANE_FAIRNESS] = 106
REVISION_RANK[ACTIVE_SELLING_QUOTE] = 107
REVISION_RANK[COMMERCIAL_PROGRESS] = 108
REVISION_RANK[EARNINGS_TOTALS] = 105
REVISION_RANK[PARALLEL_HISTORY] = 106
REVISION_RANK[HISTORY_RELEASE_MERGE] = 109
REVISION_RANK[RECENT_HISTORY] = 110
REVISION_RANK[MOXY] = 111
REVISION_RANK[VAULT_WINDOW] = 112
REVISION_RANK[ENGINE] = 113
REVISION_RANK[INDEPENDENCE] = 114
REVISION_RANK[ENGINE_MEMORY] = 115
REVISION_RANK[ENGINE_CATALOGUE] = 116
REVISION_RANK[ENGINE_PARTS] = 117
REVISION_RANK[ENGINE_OPERATOR] = 118
REVISION_RANK[ENGINE_IDENTITY] = 119
REVISION_RANK[FANS_INTAKE] = 120
REVISION_RANK[ENGINE_GENERATIONS] = 121
REVISION_RANK[FANSAPI_TRANSPORT] = 122
REVISION_RANK[ENGINE_MODEL_CALLS] = 123
REVISION_RANK[ENGINE_APPROVAL] = 124
REVISION_RANK[PLAN_C_SCOPE] = 125
REVISION_RANK[PLAN_C_INGRESS] = 126
REVISION_RANK[OFFER_GRANT_LINEAGE] = 127
REVISION_RANK[MODEL_BILLING_EVIDENCE] = 128
REVISION_RANK[RECONNECT_RECEIPT] = 129
REVISION_RANK[MODEL_RETRY_DUE] = 129
REVISION_RANK[COMMERCIAL_VAULT] = 130
REVISION_RANK[CATEGORY_PRICES] = 131
REVISION_RANK[ACCOUNT_PREPARATION] = 132
REVISION_RANK[PREPARATION_RETRY_STATE] = 133


class SchemaMismatch(RuntimeError):
    pass


def _metadata():
    from .models import Base
    from . import onlyfans_moxy_models, onlyfans_engine_models, onlyfans_fans_intake_models
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
    from . import onlyfans_contextual_models  # noqa: F401
    from . import onlyfans_case_models  # noqa: F401
    from . import onlyfans_substitution_models  # noqa: F401
    from . import onlyfans_sales_models  # noqa: F401
    from . import onlyfans_earnings_total_models  # noqa: F401
    from onlyfans_runtime import history_telemetry, history_policy  # noqa: F401
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
    # These two valid release lines use overlapping numeric prefixes.
    # Membership must follow Alembic ancestry, not just numeric rank.
    from alembic.script import ScriptDirectory
    ancestors = {r.revision for r in ScriptDirectory.from_config(_config(conn)).iterate_revisions(expected, "base")}
    def since_rank(item, *, fallback=0):
        since = item.info.get("onlyfans_since")
        return 10**9 if since and since not in ancestors else _since_rank(item, fallback=fallback)
    def table_rank(table):
        since = table.info.get("onlyfans_since")
        return 10**9 if since and since not in ancestors else _table_rank(table)
    for table in _metadata().sorted_tables:
        if table_rank(table) > expected_rank:
            continue
        if table.name not in tables:
            failures.append(f"missing table {table.name}")
            continue
        actual = {c["name"]: c for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if since_rank(column) > expected_rank:
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
            if since_rank(index) > expected_rank or any(since_rank(c) > expected_rank for c in index.columns):
                continue
            other = indexes.get(index.name)
            if other is None or other["unique"] != bool(index.unique) or other["column_names"] != [c.name for c in index.columns]:
                failures.append(f"incompatible index {index.name}")
            elif index.dialect_options["postgresql"].get("where") is not None:
                # A full unique constraint is NOT an equivalent active-binding index.
                predicate = str(other.get("dialect_options", {}).get("postgresql_where", ""))
                if not predicate or (index.name == "uq_of_creator_bindings_active_account" and predicate.replace("(", "").replace(")", "").strip() != "revoked_at IS NULL"):
                    failures.append(f"incompatible predicate {index.name}")
                if index.name in {"uq_fans_connection_active", "uq_fans_provider_account_active"}:
                    normal = predicate.replace("(", "").replace(")", "").replace("::text", "").replace("::character varying", "").strip()
                    if normal != "status = 'active'":
                        failures.append(f"incompatible predicate {index.name}")
                if index.name == "uq_engine_approval_live":
                    normal = re.sub(r"::(?:character varying|text)(?:\[\])?", "", predicate)
                    normal = re.sub(r"[\s()]", "", normal)
                    if normal != "status=ANYARRAY['awaiting','approved']":
                        failures.append(f"incompatible predicate {index.name}")
                if index.name == "uq_engine_independent_active":
                    normal = re.sub(r"::(?:character varying|text)(?:\[\])?", "", predicate)
                    normal = re.sub(r"[\s()]", "", normal)
                    if normal != "status=ANYARRAY['dispatching','unknown']":
                        failures.append(f"incompatible predicate {index.name}")
        actual_fks = {(tuple(f["constrained_columns"]), f["referred_table"], tuple(f["referred_columns"])) for f in inspector.get_foreign_keys(table.name)}
        for fk in table.foreign_key_constraints:
            if any(since_rank(c) > expected_rank for c in fk.columns):
                continue
            signature = (tuple(c.name for c in fk.columns), fk.referred_table.name, tuple(e.column.name for e in fk.elements))
            if signature not in actual_fks:
                failures.append(f"missing foreign key {table.name}")
        actual_uniques = {tuple(c["column_names"]) for c in inspector.get_unique_constraints(table.name)}
        for constraint in table.constraints:
            if constraint.__class__.__name__ == "UniqueConstraint" and since_rank(constraint) <= expected_rank and all(since_rank(c) <= expected_rank for c in constraint.columns):
                if tuple(c.name for c in constraint.columns) not in actual_uniques:
                    failures.append(f"missing unique constraint {table.name}")
        if table.name == "of_engine_decision" and 113 <= expected_rank < 117:
            if ("account_id", "request_id") not in actual_uniques:
                failures.append("missing historical engine decision uniqueness")
        if table.name == "of_content_sync_conflict" and 89 <= expected_rank < 91:
            if ("account_id", "item_id", "observation_id") not in actual_uniques:
                failures.append("missing historical unique constraint of_content_sync_conflict")
        expected_checks = {c.name for c in table.constraints if c.__class__.__name__ == "CheckConstraint" and c.name and since_rank(c) <= expected_rank}
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
    if expected_rank >= 113:
        grant_uniques = {tuple(c['column_names']) for c in inspector.get_unique_constraints('of_engine_offer_grant')}
        legacy_slot = ('account_id', 'fan_id', 'item_id', 'terms_hash')
        if (expected_rank < 127 and legacy_slot not in grant_uniques
                or expected_rank >= 127 and legacy_slot in grant_uniques):
            failures.append('incompatible offer authorization uniqueness')
    if expected_rank >= 113:
        # A matching revision is insufficient if the immutable evidence guards
        # or row security have been removed from an existing database.
        for table, trigger, digest in [
            ("of_engine_offer_grant", "of_engine_offer_grant_guard", ("af92b7c06ef0ac7f3b949eb42d9b1528f6371bb89b3caf5b68951e2f1bc4b5ac" if expected_rank >= 127 else "4047964965e2da94c621737aaf4c7a37cfb3f29a71c1f1460e33ecb02d03667f")),
            ("of_engine_access_evidence", "of_engine_access_immutable", "b1ca5e7cec1420390577f7847545d2176356f7c3240201d3149e5f19fab9d56b"),
            ("of_fans_connection", "of_fans_connection_guard", "264c6c5c69c11743c33f0596fa521dee3b782e9dc6f769e66fd99f04c118f074"),
            ("of_fans_webhook_conflict", "of_fans_webhook_conflict_immutable", "39d01166ca3144e7023fbc6732a73c9ee1866f05b9b9a9cdb8642133c85ae7e2"),
            ("of_fans_webhook", "of_fans_webhook_immutable", "8bb75b1834fff575d53602b879ee8c46244cc4f8ab640b0b130ce550e513c093"),
            ("of_engine_attempt", "of_engine_attempt_guard", "1e18c0a1170d1c166ea160c12575d140d0a0fce8638b667807c7f79cb2318c4b"),
            ("of_engine_decision", "of_engine_decision_guard", ("22bd18e3d99553ebd2bd6482457bd12dfc5287bd889ba16cb0184726bc99a954" if expected_rank >= 121 else "3806025b7b338519836a2d2d2aa52ca819c96185613179983f347383d7121b30" if expected_rank >= 117 else "1b56e33ccf1012b5e32442f732e8ac24c939e47f104313fc12d8b6b597c146c7")),
        ]:
            guard = conn.execute(text("""SELECT t.tgenabled::text,t.tgtype::integer,p.proname::text,p.prosrc::text,l.lanname::text
                FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_language l ON l.oid=p.prolang
                WHERE t.tgrelid=to_regclass(:table) AND t.tgname=:trigger AND NOT t.tgisinternal"""),
                {"table": table, "trigger": trigger}).all()
            if (len(guard) != 1 or guard[0][0] not in {"O", "A"} or guard[0][1] != (31 if table == "of_engine_offer_grant" and expected_rank >= 127 else 27)
                    or guard[0][2] != trigger or guard[0][4] != "plpgsql"
                    or hashlib.sha256(" ".join(guard[0][3].split()).encode()).hexdigest() != digest):
                failures.append(f"missing engine evidence protection {table}")
        for table in ("of_engine_owner", "of_engine_request", "of_engine_decision", "of_engine_offer_grant",
                      "of_engine_access_evidence", "of_engine_attempt", "of_fans_connection", "of_fans_webhook",
                      "of_fans_webhook_conflict"):
            if not conn.scalar(text("SELECT relrowsecurity FROM pg_class WHERE oid=to_regclass(:table)"), {"table": table}):
                failures.append(f"missing engine row security {table}")
    if expected_rank >= 114:
        for table, trigger, digest in [
            ("of_engine_independence_proof", "of_engine_independence_proof_immutable", "4ca0160d28a49c2a51220d1e4004ea563843b024dea37c5c9abf9e65ca8552d0"),
            ("of_engine_independent_admission", "of_engine_independent_admission_guard", "77c6275f4c06aafa1eadce00ddd9b21cd34eb2254c566ce356dd574a80f5dc08"),
        ]:
            guard = conn.execute(text("""SELECT t.tgenabled::text,t.tgtype::integer,p.proname::text,p.prosrc::text,l.lanname::text
                FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_language l ON l.oid=p.prolang
                WHERE t.tgrelid=to_regclass(:table) AND t.tgname=:trigger AND NOT t.tgisinternal"""),
                {"table": table, "trigger": trigger}).all()
            if (len(guard) != 1 or guard[0][0] not in {"O", "A"} or guard[0][1] != 27
                    or guard[0][2] != trigger or guard[0][4] != "plpgsql"
                    or hashlib.sha256(" ".join(guard[0][3].split()).encode()).hexdigest() != digest):
                failures.append(f"missing independent action protection {table}")
            if not conn.scalar(text("SELECT relrowsecurity FROM pg_class WHERE oid=to_regclass(:table)"), {"table": table}):
                failures.append(f"missing independent action row security {table}")
    if expected_rank >= 119:
        constraint = conn.scalar(text("""SELECT pg_get_constraintdef(oid) FROM pg_constraint
            WHERE conrelid=to_regclass('of_engine_independence_proof')
              AND conname='ck_of_engine_independence_proof_kind' AND contype='c'"""))
        normal = re.sub(r"::(?:character varying|text)(?:\[\])?", "", constraint or "")
        normal = re.sub(r"[\s()]", "", normal)
        if normal != "CHECKkind=ANYARRAY['arithmetic.v1','identity.v1']":
            failures.append('incompatible identity independence constraint')
    if expected_rank >= 115:
        table, trigger = "of_engine_memory_binding", "of_engine_memory_binding_immutable"
        guard = conn.execute(text("""SELECT t.tgenabled::text,t.tgtype::integer,p.proname::text,p.prosrc::text,l.lanname::text
            FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_language l ON l.oid=p.prolang
            WHERE t.tgrelid=to_regclass(:table) AND t.tgname=:trigger AND NOT t.tgisinternal"""),
            {"table": table, "trigger": trigger}).all()
        if (len(guard) != 1 or guard[0][0] not in {"O", "A"} or guard[0][1] != 27
                or guard[0][2] != trigger or guard[0][4] != "plpgsql"
                or hashlib.sha256(" ".join(guard[0][3].split()).encode()).hexdigest() != "e1014b600a6f1c19580a9fe213fd0c24b9ba4b85c7473958edf62c7aa9fac63c"):
            failures.append("missing engine memory binding protection")
        if not conn.scalar(text("SELECT relrowsecurity FROM pg_class WHERE oid=to_regclass(:table)"), {"table": table}):
            failures.append("missing engine memory row security")
    if expected_rank >= 116:
        table, trigger = "of_engine_catalogue_alias", "of_engine_catalogue_alias_immutable"
        guard = conn.execute(text("""SELECT t.tgenabled::text,t.tgtype::integer,p.proname::text,p.prosrc::text,l.lanname::text
            FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_language l ON l.oid=p.prolang
            WHERE t.tgrelid=to_regclass(:table) AND t.tgname=:trigger AND NOT t.tgisinternal"""),
            {"table": table, "trigger": trigger}).all()
        if (len(guard) != 1 or guard[0][0] not in {"O", "A"} or guard[0][1] != 27
                or guard[0][2] != trigger or guard[0][4] != "plpgsql"
                or hashlib.sha256(" ".join(guard[0][3].split()).encode()).hexdigest() != "06ff29df259d3fcc7819505817e3282403b166c7d34a9103e44ce7ea009526dd"):
            failures.append("missing catalogue alias protection")
        if not conn.scalar(text("SELECT relrowsecurity FROM pg_class WHERE oid=to_regclass(:table)"), {"table": table}):
            failures.append("missing catalogue alias row security")
    if expected_rank >= 118:
        table, trigger = "of_engine_operator_command", "of_engine_operator_command_immutable"
        guard = conn.execute(text("""SELECT t.tgenabled::text,t.tgtype::integer,p.proname::text,p.prosrc::text,l.lanname::text
            FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_language l ON l.oid=p.prolang
            WHERE t.tgrelid=to_regclass(:table) AND t.tgname=:trigger AND NOT t.tgisinternal"""),
            {"table": table, "trigger": trigger}).all()
        if (len(guard) != 1 or guard[0][0] not in {"O", "A"} or guard[0][1] != 27
                or guard[0][2] != trigger or guard[0][4] != "plpgsql"
                or hashlib.sha256(" ".join(guard[0][3].split()).encode()).hexdigest() != "7480e4f649e1856d20d9bb4ab2b9891f0fc208bfb3fd241b2493c3d1e436e921"):
            failures.append("missing engine operator command protection")
        if not conn.scalar(text("SELECT relrowsecurity FROM pg_class WHERE oid=to_regclass(:table)"), {"table": table}):
            failures.append("missing engine operator row security")
    if expected_rank >= 123:
        table, trigger = "of_engine_model_call", "of_engine_model_call_guard"
        guard = conn.execute(text("""SELECT t.tgenabled::text,t.tgtype::integer,p.proname::text,p.prosrc::text,l.lanname::text
            FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_language l ON l.oid=p.prolang
            WHERE t.tgrelid=to_regclass(:table) AND t.tgname=:trigger AND NOT t.tgisinternal"""),
            {"table": table, "trigger": trigger}).all()
        if (len(guard) != 1 or guard[0][0] not in {"O", "A"} or guard[0][1] != 27
                or guard[0][2] != trigger or guard[0][4] != "plpgsql"
                or hashlib.sha256(" ".join(guard[0][3].split()).encode()).hexdigest() != ("ab95da609ffe3d4915fd52aab42c61a99cd56649bac3e345ee34b41b61508cf5" if MODEL_RETRY_DUE in ancestors else "5630b811cd298311ff01ddcc0314a176be308b5d56873c50ebd780d0bdd433d8" if expected_rank >= 128 else "7f1c0f9beb768281cbd6fa8b66279f0d24f9b486890d8913de2ce690b3556a73" if expected_rank >= 125 else "9cac12064b4377f866c0b5d5022292418e8895e9e5e271c03628e528d4ac134e")):
            failures.append("missing engine model call protection")
        if not conn.scalar(text("SELECT relrowsecurity FROM pg_class WHERE oid=to_regclass(:table)"), {"table": table}):
            failures.append("missing engine model call row security")
    if expected_rank >= 124:
        table, trigger = "of_engine_approval", "of_engine_approval_guard"
        guard = conn.execute(text("""SELECT t.tgenabled::text,t.tgtype::integer,p.proname::text,p.prosrc::text,l.lanname::text
            FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_language l ON l.oid=p.prolang
            WHERE t.tgrelid=to_regclass(:table) AND t.tgname=:trigger AND NOT t.tgisinternal"""),
            {"table": table, "trigger": trigger}).all()
        if (len(guard) != 1 or guard[0][0] not in {"O", "A"} or guard[0][1] != 31  # row, before, insert|delete|update
                or guard[0][2] != trigger or guard[0][4] != "plpgsql"
                or hashlib.sha256(" ".join(guard[0][3].split()).encode()).hexdigest() != ("535d3bb84a6721a4bd2efcf1777254b01bf12f50474f2f69e45522b1e5ae469e" if expected_rank >= 125 else "03250ff630dcd19c1a14dd18e20934cf84f1592947a78fd840deb37dae020d98")):
            failures.append("missing engine approval protection")
        if not conn.scalar(text("SELECT relrowsecurity FROM pg_class WHERE oid=to_regclass(:table)"), {"table": table}):
            failures.append("missing engine approval row security")
    if expected_rank >= 126:
        metadata_fn = conn.execute(text("""SELECT p.prosrc::text,l.lanname::text,p.provolatile::text
            FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_language l ON l.oid=p.prolang
            WHERE n.nspname=current_schema() AND p.oid=to_regprocedure('of_engine_pilot_ingress_metadata(text)')""")).all()
        if (len(metadata_fn) != 1 or metadata_fn[0][1:] != ('plpgsql', 'i') or
                hashlib.sha256(" ".join(metadata_fn[0][0].split()).encode()).hexdigest() != 'fcfb7383c002bf39f9dc91b2335b74ba7c5e3001ba829ebbab043e619a1d01d7'):
            failures.append("missing safe pilot ingress metadata parser")
    if expected_rank >= 125:
        event_fn = conn.execute(text("""SELECT p.prosrc::text,l.lanname::text,p.provolatile::text
            FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_language l ON l.oid=p.prolang
            WHERE n.nspname=current_schema() AND p.oid=to_regprocedure('of_engine_pilot_event_fan(bytea,text)')""")).all()
        if (len(event_fn) != 1 or event_fn[0][1:] != ('plpgsql', 'i') or
                hashlib.sha256(" ".join(event_fn[0][0].split()).encode()).hexdigest() != "c879762ff0058c0f3b8584da72b6a5b19687f239351b730eec837f22697e6205"):
            failures.append("missing safe pilot event parser")
    if expected_rank >= 125:
        table, trigger = "of_engine_pilot_webhook_digest", "of_engine_pilot_webhook_digest_guard"
        guard = conn.execute(text("""SELECT t.tgenabled::text,t.tgtype::integer,p.proname::text,p.prosrc::text,l.lanname::text
            FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_language l ON l.oid=p.prolang
            WHERE t.tgrelid=to_regclass(:table) AND t.tgname=:trigger AND NOT t.tgisinternal"""),
            {"table": table, "trigger": trigger}).all()
        if (len(guard) != 1 or guard[0][0] not in {"O", "A"} or guard[0][1] != 27
                or guard[0][2] != trigger or guard[0][4] != "plpgsql"
                or hashlib.sha256(" ".join(guard[0][3].split()).encode()).hexdigest() != "643e5c1a1e6ad1c0ee60e28d0daae132dd018e027d4ebc6cb49a25e3c8632689"):
            failures.append("missing pilot webhook digest protection")
        if not conn.scalar(text("SELECT relrowsecurity FROM pg_class WHERE oid=to_regclass(:table)"), {"table": table}):
            failures.append("missing pilot webhook digest row security")
    if expected_rank >= 125:
        table, trigger = "of_engine_pilot_scope", "of_engine_pilot_scope_guard"
        guard = conn.execute(text("""SELECT t.tgenabled::text,t.tgtype::integer,p.proname::text,p.prosrc::text,l.lanname::text
            FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_language l ON l.oid=p.prolang
            WHERE t.tgrelid=to_regclass(:table) AND t.tgname=:trigger AND NOT t.tgisinternal"""),
            {"table": table, "trigger": trigger}).all()
        if (len(guard) != 1 or guard[0][0] not in {"O", "A"} or guard[0][1] != 31
                or guard[0][2] != trigger or guard[0][4] != "plpgsql"
                or hashlib.sha256(" ".join(guard[0][3].split()).encode()).hexdigest() != ("9acda81657e604774e5e2e6741f1d0454700ad7c233f6517fa04dc76419e33a3" if expected_rank >= 132 else "6fe99337ec51184c7d92beee2ba5abf2de83f81576aead67b3adacd39d04ace6")):
            failures.append("missing pilot scope protection")
        if not conn.scalar(text("SELECT relrowsecurity FROM pg_class WHERE oid=to_regclass(:table)"), {"table": table}):
            failures.append("missing pilot scope row security")
    if expected_rank >= 130:
        guard=conn.execute(text("""SELECT t.tgenabled::text,t.tgtype::integer,p.prosrc::text
            FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid
            WHERE t.tgrelid=to_regclass('of_vault_sync_job') AND t.tgname='of_vault_sync_provider_frozen'
            AND NOT t.tgisinternal""")).all()
        expected_source="""BEGIN
          IF ROW(NEW.provider_kind,NEW.provider_account) IS DISTINCT FROM ROW(OLD.provider_kind,OLD.provider_account)
            THEN RAISE EXCEPTION 'vault provider provenance is immutable'; END IF;
          RETURN NEW;
        END"""
        if (len(guard)!=1 or guard[0][0] not in {"O","A"} or guard[0][1]!=19
                or " ".join(guard[0][2].split())!=" ".join(expected_source.split())):
            failures.append("missing vault provider provenance protection")
    if expected_rank >= 95:
        marker = conn.execute(text("""SELECT t.tgenabled::text,t.tgtype::integer,p.prosrc::text
            FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid
            WHERE t.tgrelid=to_regclass('messages') AND t.tgname='of_content_fold_pending'
            AND NOT t.tgisinternal""")).all()
        if (len(marker) != 1 or marker[0][0] not in {"O", "A"} or marker[0][1] != 21
                or hashlib.sha256(" ".join(marker[0][2].split()).encode()).hexdigest() != "09ec9f01495ba504c532a303d68f6a7e388c3d300ef61173046edc9e3535428b"):
            failures.append("missing content change continuation trigger")
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
    if expected == CONVERSATION_DEFAULT_OFF or expected_rank >= 97:
        mode = next((c for c in inspector.get_columns("of_conversation_state") if c["name"] == "mode"), None)
        if mode is None or mode.get("default") not in {"'paused'::character varying", "'paused'::text", "'paused'"}:
            failures.append("conversation default must be paused")
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
