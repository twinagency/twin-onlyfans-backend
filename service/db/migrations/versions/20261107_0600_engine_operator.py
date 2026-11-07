"""Retain idempotent, scoped operator request decisions."""
from alembic import op
import sqlalchemy as sa

revision = '0118_engine_operator'
down_revision = '0117_engine_message_parts'
branch_labels = depends_on = None

STATEMENTS = [
    """CREATE TABLE of_engine_operator_command (
      account_id VARCHAR NOT NULL, command_key VARCHAR(64) NOT NULL, request_id VARCHAR(36) NOT NULL,
      action VARCHAR(24) NOT NULL, actor VARCHAR(128) NOT NULL, expected_revision INTEGER NOT NULL,
      target VARCHAR(128), evidence_ref VARCHAR(128), request_hash VARCHAR(64) NOT NULL, result_json TEXT NOT NULL, created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
      CONSTRAINT pk_of_engine_operator_command PRIMARY KEY(account_id,command_key),
      CONSTRAINT fk_engine_operator_request FOREIGN KEY(account_id,request_id) REFERENCES of_engine_request(account_id,request_id) ON DELETE RESTRICT,
      CONSTRAINT ck_of_engine_operator_command_action CHECK(action IN ('request_handover','accept_handover','complete','decline'))
    )""",
    """CREATE FUNCTION of_engine_operator_command_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'engine operator command is append-only'; END $$""",
    'CREATE TRIGGER of_engine_operator_command_immutable BEFORE UPDATE OR DELETE ON of_engine_operator_command FOR EACH ROW EXECUTE FUNCTION of_engine_operator_command_immutable()',
]


def upgrade():
    for statement in STATEMENTS:
        op.execute(statement)
    op.execute('ALTER TABLE of_engine_operator_command ENABLE ROW LEVEL SECURITY')
    op.execute('REVOKE ALL ON of_engine_operator_command FROM PUBLIC')
    for role in ('anon', 'authenticated'):
        op.execute(f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{role}') THEN REVOKE ALL ON of_engine_operator_command FROM {role}; END IF; END $$")


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM of_engine_operator_command LIMIT 1')).first():
        raise RuntimeError('Preserve engine operator command evidence; use a compatible rollback')
    op.execute('DROP TRIGGER of_engine_operator_command_immutable ON of_engine_operator_command')
    op.execute('DROP FUNCTION of_engine_operator_command_immutable()')
    op.drop_table('of_engine_operator_command')
