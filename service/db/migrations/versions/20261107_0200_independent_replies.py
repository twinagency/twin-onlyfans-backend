"""Preserve proven-independent synthetic replies beside an unknown action."""
from alembic import op
import sqlalchemy as sa

revision = '0114_independent_replies'
down_revision = '0113_conversation_engine'
branch_labels = depends_on = None

STATEMENTS = [
    """CREATE TABLE of_engine_independence_proof (
      account_id VARCHAR NOT NULL, decision_id VARCHAR(36) NOT NULL, fan_id BIGINT NOT NULL,
      source_message_id BIGINT NOT NULL, anchor_intent_id VARCHAR(36) NOT NULL,
      kind VARCHAR(32) NOT NULL, source_sha256 VARCHAR(64) NOT NULL, answer_sha256 VARCHAR(64) NOT NULL,
      created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
      CONSTRAINT pk_of_engine_independence_proof PRIMARY KEY(account_id,decision_id),
      CONSTRAINT fk_proof_decision FOREIGN KEY(account_id,decision_id) REFERENCES of_engine_decision(account_id,decision_id) ON DELETE RESTRICT,
      CONSTRAINT fk_proof_anchor FOREIGN KEY(account_id,anchor_intent_id) REFERENCES of_text_intent(account_id,intent_id) ON DELETE RESTRICT,
      CONSTRAINT fk_proof_source FOREIGN KEY(account_id,fan_id,source_message_id) REFERENCES messages(account_id,fan_id,message_id) ON DELETE RESTRICT,
      CONSTRAINT ck_of_engine_independence_proof_kind CHECK(kind IN ('arithmetic.v1'))
    )""",
    """CREATE TABLE of_engine_independent_admission (
      account_id VARCHAR NOT NULL, attempt_id VARCHAR(36) NOT NULL, intent_id VARCHAR(36) NOT NULL,
      anchor_intent_id VARCHAR(36) NOT NULL, fan_id BIGINT NOT NULL, status VARCHAR(16) NOT NULL,
      created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
      CONSTRAINT pk_of_engine_independent_admission PRIMARY KEY(account_id,attempt_id),
      CONSTRAINT fk_independent_intent FOREIGN KEY(account_id,intent_id) REFERENCES of_text_intent(account_id,intent_id) ON DELETE RESTRICT,
      CONSTRAINT fk_independent_anchor FOREIGN KEY(account_id,anchor_intent_id) REFERENCES of_text_intent(account_id,intent_id) ON DELETE RESTRICT,
      CONSTRAINT ck_of_engine_independent_admission_status CHECK(status IN ('dispatching','unknown','settled'))
    )""",
    "CREATE UNIQUE INDEX uq_engine_independent_active ON of_engine_independent_admission(account_id) WHERE status IN ('dispatching','unknown')",
    """CREATE FUNCTION of_engine_independence_proof_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'independence proof is append-only'; END $$""",
    'CREATE TRIGGER of_engine_independence_proof_immutable BEFORE UPDATE OR DELETE ON of_engine_independence_proof FOR EACH ROW EXECUTE FUNCTION of_engine_independence_proof_immutable()',
    """CREATE FUNCTION of_engine_independent_admission_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'independent admission evidence is retained'; END IF;
      IF ROW(NEW.account_id,NEW.attempt_id,NEW.intent_id,NEW.anchor_intent_id,NEW.fan_id,NEW.created_at)
         IS DISTINCT FROM ROW(OLD.account_id,OLD.attempt_id,OLD.intent_id,OLD.anchor_intent_id,OLD.fan_id,OLD.created_at)
         OR (OLD.status = 'settled' AND NEW.status <> 'settled')
         OR (OLD.status = 'unknown' AND NEW.status NOT IN ('unknown','settled'))
      THEN RAISE EXCEPTION 'independent admission identity is immutable'; END IF;
      RETURN NEW;
    END $$""",
    'CREATE TRIGGER of_engine_independent_admission_guard BEFORE UPDATE OR DELETE ON of_engine_independent_admission FOR EACH ROW EXECUTE FUNCTION of_engine_independent_admission_guard()',
]


def upgrade():
    for statement in STATEMENTS:
        op.execute(statement)
    for table in ('of_engine_independence_proof', 'of_engine_independent_admission'):
        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'REVOKE ALL ON {table} FROM PUBLIC')
        for role in ('anon', 'authenticated'):
            op.execute(f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{role}') THEN REVOKE ALL ON {table} FROM {role}; END IF; END $$")


def downgrade():
    conn = op.get_bind()
    for table in ('of_engine_independent_admission', 'of_engine_independence_proof'):
        if conn.execute(sa.text(f'SELECT 1 FROM {table} LIMIT 1')).first():
            raise RuntimeError('Preserve independent action evidence; use a compatible rollback')
    op.execute('DROP TRIGGER of_engine_independent_admission_guard ON of_engine_independent_admission')
    op.execute('DROP FUNCTION of_engine_independent_admission_guard()')
    op.drop_table('of_engine_independent_admission')
    op.execute('DROP TRIGGER of_engine_independence_proof_immutable ON of_engine_independence_proof')
    op.execute('DROP FUNCTION of_engine_independence_proof_immutable()')
    op.drop_table('of_engine_independence_proof')
