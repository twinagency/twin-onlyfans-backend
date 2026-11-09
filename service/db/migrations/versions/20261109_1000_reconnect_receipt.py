"""Retain signed, unbound reconnect evidence without granting authority."""
from alembic import op
from sqlalchemy import text

revision = "0129_reconnect_receipt"
down_revision = "0128_model_billing_evidence"
branch_labels = depends_on = None


def upgrade():
    op.execute("""CREATE TABLE of_engine_pilot_reconnect_receipt (
      account_id VARCHAR NOT NULL, epoch BIGINT NOT NULL, event_key VARCHAR(255) NOT NULL,
      payload_sha256 VARCHAR(64) NOT NULL, raw_body BYTEA NOT NULL, signature VARCHAR(64) NOT NULL,
      received_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
      PRIMARY KEY(account_id,epoch,event_key,payload_sha256),
      FOREIGN KEY(account_id,epoch,event_key,payload_sha256)
        REFERENCES of_engine_pilot_webhook_digest(account_id,epoch,event_key,payload_sha256) ON DELETE RESTRICT,
      CHECK (octet_length(raw_body) BETWEEN 1 AND 1000000),
      CHECK (signature ~ '^[0-9a-fA-F]{64}$')
    )""")
    op.execute("""CREATE FUNCTION of_engine_pilot_reconnect_receipt_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'pilot reconnect receipt evidence is retained'; END $$""")
    op.execute("""CREATE TRIGGER of_engine_pilot_reconnect_receipt_guard BEFORE UPDATE OR DELETE
      ON of_engine_pilot_reconnect_receipt FOR EACH ROW EXECUTE FUNCTION of_engine_pilot_reconnect_receipt_guard()""")
    op.execute('ALTER TABLE of_engine_pilot_reconnect_receipt ENABLE ROW LEVEL SECURITY')
    op.execute('REVOKE ALL ON of_engine_pilot_reconnect_receipt FROM PUBLIC')
    for role in ('anon', 'authenticated'):
        op.execute(f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{role}') THEN REVOKE ALL ON of_engine_pilot_reconnect_receipt FROM {role}; END IF; END $$")


def downgrade():
    if op.get_bind().execute(text('SELECT 1 FROM of_engine_pilot_reconnect_receipt LIMIT 1')).first():
        raise RuntimeError('Preserve reconnect receipts; use a compatible rollback')
    op.execute('DROP TRIGGER of_engine_pilot_reconnect_receipt_guard ON of_engine_pilot_reconnect_receipt')
    op.execute('DROP FUNCTION of_engine_pilot_reconnect_receipt_guard()')
    op.execute('DROP TABLE of_engine_pilot_reconnect_receipt')
