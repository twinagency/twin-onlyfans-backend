"""Add Fans API link, projection, payment evidence and backfill state."""
from alembic import op
import sqlalchemy as sa

revision = '0120_fans_intake'
down_revision = '0119_identity_independence'
branch_labels = depends_on = None

STATEMENTS = [
    """CREATE TABLE of_fans_link (
      account_id VARCHAR NOT NULL, creator_id VARCHAR(255) NOT NULL, provider_account_id VARCHAR(255) NOT NULL,
      state VARCHAR(16) NOT NULL, send_blocked BOOLEAN NOT NULL, block_reason VARCHAR(64),
      session_refreshes INTEGER NOT NULL, reconnect_count INTEGER NOT NULL,
      last_event_at TIMESTAMP WITHOUT TIME ZONE, last_event_type VARCHAR(80), updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
      CONSTRAINT pk_of_fans_link PRIMARY KEY (account_id,creator_id),
      CONSTRAINT fk_of_fans_link_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime(account_id) ON DELETE RESTRICT,
      CONSTRAINT ck_of_fans_link_state CHECK(state IN ('connected','auth_failed','disconnected')),
      CONSTRAINT ck_of_fans_link_reconnect_count CHECK(reconnect_count >= 0)
    )""",
    """CREATE TABLE of_fans_projection (
      account_id VARCHAR NOT NULL, epoch BIGINT NOT NULL, event_key VARCHAR(255) NOT NULL,
      status VARCHAR(16) NOT NULL, detail VARCHAR(96), attempts INTEGER NOT NULL, updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
      CONSTRAINT pk_of_fans_projection PRIMARY KEY (account_id,epoch,event_key),
      CONSTRAINT fk_of_fans_projection_account_id_of_fans_webhook FOREIGN KEY(account_id,epoch,event_key) REFERENCES of_fans_webhook(account_id,epoch,event_key) ON DELETE RESTRICT,
      CONSTRAINT ck_of_fans_projection_status CHECK(status IN ('pending','projected','held','ignored'))
    )""",
    """CREATE TABLE of_fans_evidence (
      account_id VARCHAR NOT NULL, evidence_id VARCHAR(36) NOT NULL, dedupe_key VARCHAR(128) NOT NULL,
      kind VARCHAR(16) NOT NULL, source VARCHAR(16) NOT NULL, event_key VARCHAR(255), fan_id BIGINT NOT NULL,
      message_id BIGINT, provider_transaction_id VARCHAR(64), amount_cents BIGINT, provider_status VARCHAR(24),
      occurred_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, status VARCHAR(16) NOT NULL, reason VARCHAR(96),
      peer_id VARCHAR(36), intent_id VARCHAR(36), grant_id VARCHAR(36), raw_sha256 VARCHAR(64) NOT NULL,
      resolved_by VARCHAR(128), created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
      CONSTRAINT pk_of_fans_evidence PRIMARY KEY (account_id,evidence_id),
      CONSTRAINT uq_fans_evidence_dedupe UNIQUE(account_id,dedupe_key),
      CONSTRAINT ck_of_fans_evidence_kind CHECK(kind IN ('unlock','transaction','chargeback','human_sent')),
      CONSTRAINT ck_of_fans_evidence_status CHECK(status IN ('pending','paired','applied','held','dismissed'))
    )""",
    'CREATE INDEX ix_fans_evidence_match ON of_fans_evidence (account_id,fan_id,kind,status)',
    """CREATE TABLE of_fans_backfill (
      account_id VARCHAR NOT NULL, run_id VARCHAR(36) NOT NULL, reason VARCHAR(32) NOT NULL,
      start_date TIMESTAMP WITHOUT TIME ZONE NOT NULL, status VARCHAR(24) NOT NULL,
      pages_used INTEGER NOT NULL, pages_budget INTEGER NOT NULL, marker TEXT,
      rows_seen INTEGER NOT NULL, rows_new INTEGER NOT NULL, detail VARCHAR(96),
      started_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
      CONSTRAINT pk_of_fans_backfill PRIMARY KEY (account_id,run_id),
      CONSTRAINT fk_of_fans_backfill_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime(account_id) ON DELETE RESTRICT,
      CONSTRAINT ck_of_fans_backfill_status CHECK(status IN ('running','complete','budget_exhausted','failed')),
      CONSTRAINT ck_of_fans_backfill_pages CHECK(pages_used >= 0 AND pages_budget > 0)
    )""",
]
TABLES = ('of_fans_backfill', 'of_fans_evidence', 'of_fans_projection', 'of_fans_link')


def upgrade():
    for statement in STATEMENTS:
        op.execute(statement)
    for table in TABLES:
        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'REVOKE ALL ON {table} FROM PUBLIC')
        for role in ('anon', 'authenticated'):
            op.execute(f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{role}') THEN REVOKE ALL ON {table} FROM {role}; END IF; END $$")


def downgrade():
    for table in ('of_fans_link', 'of_fans_projection', 'of_fans_evidence', 'of_fans_backfill'):
        if op.get_bind().execute(sa.text(f'SELECT 1 FROM {table} LIMIT 1')).first():
            raise RuntimeError('Preserve Fans API intake evidence; use a compatible rollback')
    for table in TABLES:
        op.drop_table(table)
