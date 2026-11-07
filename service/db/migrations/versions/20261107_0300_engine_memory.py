"""Bind each engine decision to the shared scoped memory projection."""
from alembic import op
import sqlalchemy as sa

revision = '0115_engine_memory'
down_revision = '0114_independent_replies'
branch_labels = depends_on = None

STATEMENTS = [
    """CREATE TABLE of_engine_memory_binding (
      account_id VARCHAR NOT NULL, decision_id VARCHAR(36) NOT NULL, fan_id BIGINT NOT NULL,
      source_message_id BIGINT NOT NULL, binding_id VARCHAR NOT NULL, source_sha256 VARCHAR(64) NOT NULL, status VARCHAR(16) NOT NULL,
      fingerprint VARCHAR(64) NOT NULL, offer_fingerprint VARCHAR(64) NOT NULL, created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
      CONSTRAINT pk_of_engine_memory_binding PRIMARY KEY(account_id,decision_id),
      CONSTRAINT fk_engine_memory_decision FOREIGN KEY(account_id,decision_id) REFERENCES of_engine_decision(account_id,decision_id) ON DELETE RESTRICT,
      CONSTRAINT fk_engine_memory_source FOREIGN KEY(account_id,fan_id,source_message_id) REFERENCES messages(account_id,fan_id,message_id) ON DELETE RESTRICT,
      CONSTRAINT fk_engine_memory_binding FOREIGN KEY(binding_id) REFERENCES of_creator_bindings(id) ON DELETE RESTRICT,
      CONSTRAINT ck_of_engine_memory_binding_status CHECK(status IN ('current','unavailable'))
    )""",
    """CREATE FUNCTION of_engine_memory_binding_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'engine memory binding is append-only'; END $$""",
    'CREATE TRIGGER of_engine_memory_binding_immutable BEFORE UPDATE OR DELETE ON of_engine_memory_binding FOR EACH ROW EXECUTE FUNCTION of_engine_memory_binding_immutable()',
]


def upgrade():
    for statement in STATEMENTS:
        op.execute(statement)
    op.execute('ALTER TABLE of_engine_memory_binding ENABLE ROW LEVEL SECURITY')
    op.execute('REVOKE ALL ON of_engine_memory_binding FROM PUBLIC')
    for role in ('anon', 'authenticated'):
        op.execute(f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{role}') THEN REVOKE ALL ON of_engine_memory_binding FROM {role}; END IF; END $$")


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM of_engine_memory_binding LIMIT 1')).first():
        raise RuntimeError('Preserve engine memory binding evidence; use a compatible rollback')
    op.execute('DROP TRIGGER of_engine_memory_binding_immutable ON of_engine_memory_binding')
    op.execute('DROP FUNCTION of_engine_memory_binding_immutable()')
    op.drop_table('of_engine_memory_binding')
