"""History and ownership coverage for account preparation. Additive only.

Five account-keyed tables: import source state/sub-budget, per-fan history cursor, retained gaps (failed
pages and quarantined rows), first-seen message facts and an hourly read meter. Nothing is ever deleted or
rewritten: one trigger refuses every DELETE and a second freezes message facts except for the one legitimate
transition (a purchase later observed as opened).

REVIEWER NOTE: this branch sets ``down_revision`` to ``0133_preparation_retry_state`` because the sibling
revisions 0134_commercial_scope_contract and 0135_commercial_catalogue_terms are built on separate branches.
The owner re-parents this revision onto 0135 at merge; nothing here depends on those revisions.
"""
from alembic import op
import sqlalchemy as sa

revision = "0136_history_coverage"
down_revision = "0135_commercial_catalogue_terms"  # integration: step 1 re-parented onto step 3
branch_labels = depends_on = None

TABLES = ("of_fans_import_source", "of_fans_history_cursor", "of_fans_import_gap", "of_fans_message_fact",
          "of_fans_read_meter")
RETAINED = "CREATE OR REPLACE FUNCTION of_fans_history_retained() RETURNS trigger LANGUAGE plpgsql AS $$\nBEGIN\n  RAISE EXCEPTION 'history import evidence is retained';\nEND\n$$"
FACT_GUARD = "CREATE OR REPLACE FUNCTION of_fans_message_fact_guard() RETURNS trigger LANGUAGE plpgsql AS $$\nBEGIN\n  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'history import evidence is retained'; END IF;\n  IF ROW(NEW.account_id,NEW.fan_id,NEW.message_id,NEW.price_cents,NEW.media_ids_json,NEW.preview_ids_json,NEW.locked_text,\n         NEW.from_queue,NEW.is_tip,NEW.via,NEW.raw_sha256,NEW.sent_at,NEW.created_at)\n     IS DISTINCT FROM ROW(OLD.account_id,OLD.fan_id,OLD.message_id,OLD.price_cents,OLD.media_ids_json,OLD.preview_ids_json,OLD.locked_text,\n         OLD.from_queue,OLD.is_tip,OLD.via,OLD.raw_sha256,OLD.sent_at,OLD.created_at)\n     OR (OLD.is_opened IS TRUE AND NEW.is_opened IS DISTINCT FROM TRUE)\n     OR (OLD.conflicted AND NOT NEW.conflicted)\n  THEN RAISE EXCEPTION 'message facts are immutable'; END IF;\n  RETURN NEW;\nEND\n$$"

STATEMENTS = [
    """CREATE TABLE of_fans_import_source (
      account_id VARCHAR NOT NULL, source VARCHAR(16) NOT NULL, state VARCHAR(16) NOT NULL, revision INTEGER NOT NULL,
      boundary_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, cursor_json TEXT NOT NULL,
      rows_seen INTEGER DEFAULT 0 NOT NULL, rows_new INTEGER DEFAULT 0 NOT NULL, rows_duplicate INTEGER DEFAULT 0 NOT NULL,
      rows_conflict INTEGER DEFAULT 0 NOT NULL, rows_ignored INTEGER DEFAULT 0 NOT NULL, rows_quarantined INTEGER DEFAULT 0 NOT NULL,
      rows_late INTEGER DEFAULT 0 NOT NULL, pages_ok INTEGER DEFAULT 0 NOT NULL, pages_failed INTEGER DEFAULT 0 NOT NULL,
      requests_budget BIGINT NOT NULL, credits_budget BIGINT NOT NULL, requests_used BIGINT DEFAULT 0 NOT NULL,
      credits_reserved BIGINT DEFAULT 0 NOT NULL, min_interval_seconds INTEGER NOT NULL, retry_attempts INTEGER DEFAULT 0 NOT NULL,
      next_read_at TIMESTAMP WITHOUT TIME ZONE, last_read_at TIMESTAMP WITHOUT TIME ZONE, completed_at TIMESTAMP WITHOUT TIME ZONE,
      last_error VARCHAR(64), evidence_json TEXT NOT NULL,
      created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
      CONSTRAINT pk_of_fans_import_source PRIMARY KEY (account_id,source),
      CONSTRAINT fk_of_fans_import_source_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime(account_id) ON DELETE RESTRICT,
      CONSTRAINT ck_of_fans_import_source_source CHECK(source IN ('vault_window','chats','transactions','chargebacks')),
      CONSTRAINT ck_of_fans_import_source_state CHECK(state IN ('not_started','running','partial','complete','failed')),
      CONSTRAINT ck_of_fans_import_source_counters CHECK(rows_seen >= 0 AND rows_new >= 0 AND rows_duplicate >= 0 AND rows_conflict >= 0 AND rows_ignored >= 0 AND rows_quarantined >= 0 AND rows_late >= 0 AND pages_ok >= 0 AND pages_failed >= 0 AND retry_attempts >= 0 AND revision > 0),
      CONSTRAINT ck_of_fans_import_source_reconcile CHECK(rows_seen = rows_new + rows_duplicate + rows_conflict + rows_ignored + rows_quarantined),
      CONSTRAINT ck_of_fans_import_source_late CHECK(rows_late <= rows_seen),
      CONSTRAINT ck_of_fans_import_source_budget CHECK(requests_budget >= 0 AND credits_budget >= 0 AND requests_used >= 0 AND credits_reserved >= 0 AND requests_used <= requests_budget AND credits_reserved <= credits_budget),
      CONSTRAINT ck_of_fans_import_source_interval CHECK(min_interval_seconds >= 0 AND min_interval_seconds <= 86400)
    )""",
    """CREATE TABLE of_fans_history_cursor (
      account_id VARCHAR NOT NULL, fan_id BIGINT NOT NULL, status VARCHAR(12) NOT NULL, priority INTEGER NOT NULL,
      next_page TEXT, first_page_at TIMESTAMP WITHOUT TIME ZONE, completed_at TIMESTAMP WITHOUT TIME ZONE,
      newest_message_id BIGINT, oldest_message_at TIMESTAMP WITHOUT TIME ZONE,
      pages INTEGER DEFAULT 0 NOT NULL, messages_seen INTEGER DEFAULT 0 NOT NULL, retry_attempts INTEGER DEFAULT 0 NOT NULL,
      retry_not_before TIMESTAMP WITHOUT TIME ZONE, last_error VARCHAR(64),
      created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
      CONSTRAINT pk_of_fans_history_cursor PRIMARY KEY (account_id,fan_id),
      CONSTRAINT fk_of_fans_history_cursor_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime(account_id) ON DELETE RESTRICT,
      CONSTRAINT ck_of_fans_history_cursor_status CHECK(status IN ('needed','running','complete','gapped')),
      CONSTRAINT ck_of_fans_history_cursor_priority CHECK(priority BETWEEN 0 AND 3),
      CONSTRAINT ck_of_fans_history_cursor_counters CHECK(pages >= 0 AND messages_seen >= 0 AND retry_attempts >= 0),
      CONSTRAINT ck_of_fans_history_cursor_completed CHECK((status = 'complete') = (completed_at IS NOT NULL))
    )""",
    "CREATE INDEX ix_of_fans_history_cursor_work ON of_fans_history_cursor (account_id,status,priority,fan_id)",
    """CREATE TABLE of_fans_import_gap (
      account_id VARCHAR NOT NULL, gap_id VARCHAR(40) NOT NULL, source VARCHAR(16) NOT NULL, fan_id BIGINT,
      kind VARCHAR(8) NOT NULL, cursor_ref TEXT, reason_code VARCHAR(64) NOT NULL, detail VARCHAR(96), http_status INTEGER,
      attempts INTEGER DEFAULT 0 NOT NULL, exhausted BOOLEAN DEFAULT false NOT NULL, raw_sha256 VARCHAR(64),
      created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
      resolved_at TIMESTAMP WITHOUT TIME ZONE, resolved_by VARCHAR(128), resolution VARCHAR(24),
      CONSTRAINT pk_of_fans_import_gap PRIMARY KEY (account_id,gap_id),
      CONSTRAINT fk_of_fans_import_gap_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime(account_id) ON DELETE RESTRICT,
      CONSTRAINT ck_of_fans_import_gap_source CHECK(source IN ('chats','transactions','chargebacks')),
      CONSTRAINT ck_of_fans_import_gap_kind CHECK(kind IN ('page','row')),
      CONSTRAINT ck_of_fans_import_gap_attempts CHECK(attempts >= 0),
      CONSTRAINT ck_of_fans_import_gap_resolution CHECK((resolved_at IS NULL) = (resolved_by IS NULL))
    )""",
    "CREATE INDEX ix_of_fans_import_gap_open ON of_fans_import_gap (account_id,source,fan_id,resolved_at)",
    """CREATE TABLE of_fans_message_fact (
      account_id VARCHAR NOT NULL, fan_id BIGINT NOT NULL, message_id BIGINT NOT NULL, price_cents INTEGER NOT NULL,
      media_ids_json TEXT NOT NULL, preview_ids_json TEXT, locked_text BOOLEAN, from_queue BOOLEAN, is_opened BOOLEAN,
      opened_at TIMESTAMP WITHOUT TIME ZONE, is_tip BOOLEAN, conflicted BOOLEAN DEFAULT false NOT NULL,
      via VARCHAR(8) NOT NULL, raw_sha256 VARCHAR(64) NOT NULL,
      sent_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
      CONSTRAINT pk_of_fans_message_fact PRIMARY KEY (account_id,fan_id,message_id),
      CONSTRAINT fk_of_fans_message_fact_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime(account_id) ON DELETE RESTRICT,
      CONSTRAINT ck_of_fans_message_fact_shape CHECK(price_cents > 0 AND via IN ('history','webhook'))
    )""",
    "CREATE INDEX ix_of_fans_message_fact_fan ON of_fans_message_fact (account_id,fan_id)",
    """CREATE TABLE of_fans_read_meter (
      account_id VARCHAR NOT NULL, source VARCHAR(16) NOT NULL, hour TIMESTAMP WITHOUT TIME ZONE NOT NULL,
      requests INTEGER DEFAULT 0 NOT NULL, ok INTEGER DEFAULT 0 NOT NULL, rate_limited INTEGER DEFAULT 0 NOT NULL,
      failed INTEGER DEFAULT 0 NOT NULL, credits_reserved BIGINT DEFAULT 0 NOT NULL, rows_read INTEGER DEFAULT 0 NOT NULL,
      balance_first BIGINT, balance_last BIGINT, updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
      CONSTRAINT pk_of_fans_read_meter PRIMARY KEY (account_id,source,hour),
      CONSTRAINT fk_of_fans_read_meter_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime(account_id) ON DELETE RESTRICT,
      CONSTRAINT ck_of_fans_read_meter_counters CHECK(requests >= 0 AND ok >= 0 AND rate_limited >= 0 AND failed >= 0 AND credits_reserved >= 0 AND rows_read >= 0 AND ok + rate_limited + failed <= requests)
    )""",
]


def upgrade():
    for statement in STATEMENTS:
        op.execute(statement)
    op.execute(RETAINED)
    op.execute(FACT_GUARD)
    for table in TABLES:
        trigger = table + "_retained"
        if table == "of_fans_message_fact":
            op.execute(f"CREATE TRIGGER {trigger} BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION of_fans_message_fact_guard()")
        else:
            op.execute(f"CREATE TRIGGER {trigger} BEFORE DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION of_fans_history_retained()")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON {table} FROM PUBLIC")
        for role in ("anon", "authenticated"):
            op.execute(f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{role}') THEN REVOKE ALL ON {table} FROM {role}; END IF; END $$")


def downgrade():
    bind = op.get_bind()
    for table in TABLES:
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("Preserve history coverage evidence; use a compatible code rollback")
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE {table}")
    op.execute("DROP FUNCTION of_fans_message_fact_guard()")
    op.execute("DROP FUNCTION of_fans_history_retained()")
