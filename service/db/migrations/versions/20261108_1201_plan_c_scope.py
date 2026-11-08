"""Durable singleton Plan C scope, preserving existing delivery and model ledgers."""
from alembic import op
import sqlalchemy as sa

revision = '0125_plan_c_scope'
down_revision = '0124_engine_approval'
branch_labels = depends_on = None

MODEL_GUARD = "CREATE OR REPLACE FUNCTION of_engine_model_call_guard() RETURNS trigger LANGUAGE plpgsql AS $$\nBEGIN\n  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'engine model call evidence is retained'; END IF;\n  IF ROW(NEW.account_id,NEW.call_id,NEW.request_id,NEW.generation,NEW.seq,NEW.chain_pos,NEW.chain_step,NEW.fan_id,NEW.binding_id,\n         NEW.owner_epoch,NEW.provider,NEW.model,NEW.prefixed,NEW.reserved_microusd,NEW.snapshot_sha256,NEW.prompt_sha256,NEW.cost_day,NEW.created_at,NEW.pilot_scope_revision,NEW.pilot_scope_fingerprint)\n     IS DISTINCT FROM ROW(OLD.account_id,OLD.call_id,OLD.request_id,OLD.generation,OLD.seq,OLD.chain_pos,OLD.chain_step,OLD.fan_id,OLD.binding_id,\n         OLD.owner_epoch,OLD.provider,OLD.model,OLD.prefixed,OLD.reserved_microusd,OLD.snapshot_sha256,OLD.prompt_sha256,OLD.cost_day,OLD.created_at,OLD.pilot_scope_revision,OLD.pilot_scope_fingerprint)\n     OR (OLD.status = 'settled' AND ROW(NEW.status,NEW.actual_microusd,NEW.usage_known,NEW.response_sha256) IS DISTINCT FROM ROW(OLD.status,OLD.actual_microusd,OLD.usage_known,OLD.response_sha256))\n     OR (OLD.status = 'released' AND NEW.status <> 'released')\n     OR (OLD.status = 'unknown' AND NEW.status NOT IN ('unknown','settled'))\n     OR (OLD.status = 'calling' AND NEW.status = 'reserved')\n  THEN RAISE EXCEPTION 'engine model call identity is immutable'; END IF;\n  RETURN NEW;\nEND $$"
OLD_MODEL_GUARD = "CREATE OR REPLACE FUNCTION of_engine_model_call_guard() RETURNS trigger LANGUAGE plpgsql AS $$\nBEGIN\n  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'engine model call evidence is retained'; END IF;\n  IF ROW(NEW.account_id,NEW.call_id,NEW.request_id,NEW.generation,NEW.seq,NEW.chain_pos,NEW.chain_step,NEW.fan_id,NEW.binding_id,\n         NEW.owner_epoch,NEW.provider,NEW.model,NEW.prefixed,NEW.reserved_microusd,NEW.snapshot_sha256,NEW.prompt_sha256,NEW.cost_day,NEW.created_at)\n     IS DISTINCT FROM ROW(OLD.account_id,OLD.call_id,OLD.request_id,OLD.generation,OLD.seq,OLD.chain_pos,OLD.chain_step,OLD.fan_id,OLD.binding_id,\n         OLD.owner_epoch,OLD.provider,OLD.model,OLD.prefixed,OLD.reserved_microusd,OLD.snapshot_sha256,OLD.prompt_sha256,OLD.cost_day,OLD.created_at)\n     OR (OLD.status = 'settled' AND ROW(NEW.status,NEW.actual_microusd,NEW.usage_known,NEW.response_sha256) IS DISTINCT FROM ROW(OLD.status,OLD.actual_microusd,OLD.usage_known,OLD.response_sha256))\n     OR (OLD.status = 'released' AND NEW.status <> 'released')\n     OR (OLD.status = 'unknown' AND NEW.status NOT IN ('unknown','settled'))\n     OR (OLD.status = 'calling' AND NEW.status = 'reserved')\n  THEN RAISE EXCEPTION 'engine model call identity is immutable'; END IF;\n  RETURN NEW;\nEND $$"

TABLE = """CREATE TABLE of_engine_pilot_scope (
  scope_id INTEGER NOT NULL PRIMARY KEY, account_id VARCHAR NOT NULL UNIQUE, binding_id VARCHAR NOT NULL,
  revision INTEGER NOT NULL, status VARCHAR(16) NOT NULL, active_since TIMESTAMP WITHOUT TIME ZONE, config_json TEXT NOT NULL, fingerprint VARCHAR(64) NOT NULL,
  provider_requests_used BIGINT NOT NULL, provider_credits_reserved BIGINT NOT NULL,
  created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
  FOREIGN KEY(account_id) REFERENCES of_runtime(account_id) ON DELETE RESTRICT,
  FOREIGN KEY(binding_id) REFERENCES of_creator_bindings(id) ON DELETE RESTRICT,
  CONSTRAINT ck_of_engine_pilot_scope_singleton CHECK(scope_id = 1),
  CONSTRAINT ck_of_engine_pilot_scope_status CHECK(status IN ('configured','active','stopped')),
  CONSTRAINT ck_of_engine_pilot_scope_active_since CHECK((status = 'active') = (active_since IS NOT NULL)),
  CONSTRAINT ck_of_engine_pilot_scope_numbers CHECK(revision > 0 AND provider_requests_used >= 0 AND provider_credits_reserved >= 0)
)"""
GUARD = """CREATE FUNCTION of_engine_pilot_scope_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'pilot scope is retained'; END IF;
  IF TG_OP = 'INSERT' THEN
    IF NEW.scope_id <> 1 OR NEW.revision <> 1 OR NEW.status <> 'configured'
       OR NEW.provider_requests_used <> 0 OR NEW.provider_credits_reserved <> 0 THEN
      RAISE EXCEPTION 'pilot scope must start configured and unused'; END IF;
    RETURN NEW;
  END IF;
  IF ROW(NEW.scope_id,NEW.account_id,NEW.binding_id,NEW.created_at)
     IS DISTINCT FROM ROW(OLD.scope_id,OLD.account_id,OLD.binding_id,OLD.created_at)
     OR NEW.provider_requests_used < OLD.provider_requests_used
     OR NEW.provider_credits_reserved < OLD.provider_credits_reserved THEN
    RAISE EXCEPTION 'pilot identity and reservations are retained'; END IF;
  IF ROW(NEW.status,NEW.active_since,NEW.config_json,NEW.fingerprint) IS DISTINCT FROM ROW(OLD.status,OLD.active_since,OLD.config_json,OLD.fingerprint) THEN
    IF NEW.revision <> OLD.revision + 1 OR
       (OLD.status = 'active' AND ROW(NEW.config_json,NEW.fingerprint) IS DISTINCT FROM ROW(OLD.config_json,OLD.fingerprint)) THEN
      RAISE EXCEPTION 'pilot configuration transition is invalid'; END IF;
  ELSIF NEW.revision <> OLD.revision THEN
    RAISE EXCEPTION 'pilot revision changed without a configuration transition';
  END IF;
  RETURN NEW;
END
$$"""


def upgrade():
    op.add_column('of_engine_model_call', sa.Column('pilot_scope_revision', sa.Integer(), nullable=True))
    op.add_column('of_engine_model_call', sa.Column('pilot_scope_fingerprint', sa.String(64), nullable=True))
    op.execute(MODEL_GUARD)
    op.execute(TABLE)
    op.execute(GUARD)
    op.execute('CREATE TRIGGER of_engine_pilot_scope_guard BEFORE INSERT OR UPDATE OR DELETE ON of_engine_pilot_scope FOR EACH ROW EXECUTE FUNCTION of_engine_pilot_scope_guard()')
    op.execute('ALTER TABLE of_engine_pilot_scope ENABLE ROW LEVEL SECURITY')
    op.execute('REVOKE ALL ON of_engine_pilot_scope FROM PUBLIC')
    for role in ('anon', 'authenticated'):
        op.execute(f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{role}') THEN REVOKE ALL ON of_engine_pilot_scope FROM {role}; END IF; END $$")


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM of_engine_pilot_scope LIMIT 1')).first():
        raise RuntimeError('Preserve pilot configuration and reservations; use a compatible rollback')
    op.execute('DROP TRIGGER of_engine_pilot_scope_guard ON of_engine_pilot_scope')
    op.execute('DROP FUNCTION of_engine_pilot_scope_guard()')
    op.drop_table('of_engine_pilot_scope')
    op.execute(OLD_MODEL_GUARD)
    op.drop_column('of_engine_model_call', 'pilot_scope_fingerprint')
    op.drop_column('of_engine_model_call', 'pilot_scope_revision')
