"""Durable model-call records: reservation, lease, settlement and conservative unknown usage.

Chain: ...0119 -> 0120_fans_intake -> 0121_engine_generations -> 0122_fansapi_transport -> 0123_engine_model_calls (this file).
"""
from alembic import op
import sqlalchemy as sa

revision = '0123_engine_model_calls'
down_revision = '0122_fansapi_transport'
branch_labels = depends_on = None

TABLE = """CREATE TABLE of_engine_model_call (
  account_id VARCHAR NOT NULL, call_id VARCHAR(36) NOT NULL, request_id VARCHAR(36) NOT NULL,
  generation INTEGER NOT NULL, seq INTEGER NOT NULL, chain_pos INTEGER NOT NULL, chain_step VARCHAR(16) NOT NULL,
  fan_id BIGINT NOT NULL, binding_id VARCHAR NOT NULL, owner_epoch BIGINT NOT NULL,
  provider VARCHAR(32) NOT NULL, model VARCHAR(128) NOT NULL, prefixed BOOLEAN NOT NULL,
  status VARCHAR(16) NOT NULL, outcome VARCHAR(24),
  reserved_microusd BIGINT NOT NULL, actual_microusd BIGINT, usage_known BOOLEAN NOT NULL DEFAULT false,
  prompt_tokens INTEGER, completion_tokens INTEGER,
  lease_owner VARCHAR(64) NOT NULL, lease_expires_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, attempt INTEGER NOT NULL DEFAULT 1,
  snapshot_sha256 VARCHAR(64) NOT NULL, prompt_sha256 VARCHAR(64) NOT NULL,
  response_text TEXT, response_sha256 VARCHAR(64), error_class VARCHAR(64),
  cost_day DATE NOT NULL,
  created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
  wire_started_at TIMESTAMP WITHOUT TIME ZONE, settled_at TIMESTAMP WITHOUT TIME ZONE,
  CONSTRAINT pk_of_engine_model_call PRIMARY KEY(account_id, call_id),
  CONSTRAINT uq_engine_model_call_request_seq UNIQUE(account_id, request_id, generation, seq),
  CONSTRAINT fk_engine_model_call_request FOREIGN KEY(account_id, request_id) REFERENCES of_engine_request(account_id, request_id) ON DELETE RESTRICT,
  CONSTRAINT fk_engine_model_call_binding FOREIGN KEY(binding_id) REFERENCES of_creator_bindings(id) ON DELETE RESTRICT,
  CONSTRAINT ck_of_engine_model_call_status CHECK(status IN ('reserved','calling','settled','released','unknown')),
  CONSTRAINT ck_of_engine_model_call_amounts CHECK(reserved_microusd >= 0 AND (actual_microusd IS NULL OR actual_microusd >= 0)
      AND generation >= 0 AND seq >= 0 AND chain_pos >= 0 AND attempt >= 1),
  CONSTRAINT ck_of_engine_model_call_settled CHECK(status <> 'settled' OR (usage_known AND actual_microusd IS NOT NULL))
)"""

GUARD = """CREATE FUNCTION of_engine_model_call_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'engine model call evidence is retained'; END IF;
  IF ROW(NEW.account_id,NEW.call_id,NEW.request_id,NEW.generation,NEW.seq,NEW.chain_pos,NEW.chain_step,NEW.fan_id,NEW.binding_id,
         NEW.owner_epoch,NEW.provider,NEW.model,NEW.prefixed,NEW.reserved_microusd,NEW.snapshot_sha256,NEW.prompt_sha256,NEW.cost_day,NEW.created_at)
     IS DISTINCT FROM ROW(OLD.account_id,OLD.call_id,OLD.request_id,OLD.generation,OLD.seq,OLD.chain_pos,OLD.chain_step,OLD.fan_id,OLD.binding_id,
         OLD.owner_epoch,OLD.provider,OLD.model,OLD.prefixed,OLD.reserved_microusd,OLD.snapshot_sha256,OLD.prompt_sha256,OLD.cost_day,OLD.created_at)
     OR (OLD.status = 'settled' AND ROW(NEW.status,NEW.actual_microusd,NEW.usage_known,NEW.response_sha256) IS DISTINCT FROM ROW(OLD.status,OLD.actual_microusd,OLD.usage_known,OLD.response_sha256))
     OR (OLD.status = 'released' AND NEW.status <> 'released')
     OR (OLD.status = 'unknown' AND NEW.status NOT IN ('unknown','settled'))
     OR (OLD.status = 'calling' AND NEW.status = 'reserved')
  THEN RAISE EXCEPTION 'engine model call identity is immutable'; END IF;
  RETURN NEW;
END $$"""

INDEXES = [
    'CREATE INDEX ix_engine_model_call_day ON of_engine_model_call(cost_day, account_id)',
    'CREATE INDEX ix_engine_model_call_open ON of_engine_model_call(account_id, request_id, generation, status)',
]


def upgrade():
    op.execute(TABLE)
    for statement in INDEXES:
        op.execute(statement)
    op.execute(GUARD)
    op.execute('CREATE TRIGGER of_engine_model_call_guard BEFORE UPDATE OR DELETE ON of_engine_model_call FOR EACH ROW EXECUTE FUNCTION of_engine_model_call_guard()')
    op.execute('ALTER TABLE of_engine_model_call ENABLE ROW LEVEL SECURITY')
    op.execute('REVOKE ALL ON of_engine_model_call FROM PUBLIC')
    for role in ('anon', 'authenticated'):
        op.execute(f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{role}') THEN REVOKE ALL ON of_engine_model_call FROM {role}; END IF; END $$")


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM of_engine_model_call LIMIT 1')).first():
        raise RuntimeError('Preserve engine model call evidence; use a compatible rollback')
    op.execute('DROP TRIGGER of_engine_model_call_guard ON of_engine_model_call')
    op.execute('DROP FUNCTION of_engine_model_call_guard()')
    op.drop_table('of_engine_model_call')
