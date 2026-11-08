"""Durable singleton Plan C scope, preserving existing delivery and model ledgers."""
from alembic import op
import sqlalchemy as sa

revision = '0125_plan_c_scope'
down_revision = '0124_engine_approval'
branch_labels = depends_on = None

TABLE = """CREATE TABLE of_engine_pilot_scope (
  scope_id INTEGER NOT NULL PRIMARY KEY, account_id VARCHAR NOT NULL UNIQUE, binding_id VARCHAR NOT NULL,
  revision INTEGER NOT NULL, status VARCHAR(16) NOT NULL, config_json TEXT NOT NULL, fingerprint VARCHAR(64) NOT NULL,
  provider_requests_used BIGINT NOT NULL, provider_credits_reserved BIGINT NOT NULL,
  created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
  FOREIGN KEY(account_id) REFERENCES of_runtime(account_id) ON DELETE RESTRICT,
  FOREIGN KEY(binding_id) REFERENCES of_creator_bindings(id) ON DELETE RESTRICT,
  CONSTRAINT ck_of_engine_pilot_scope_singleton CHECK(scope_id = 1),
  CONSTRAINT ck_of_engine_pilot_scope_status CHECK(status IN ('configured','active','stopped')),
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
  IF ROW(NEW.status,NEW.config_json,NEW.fingerprint) IS DISTINCT FROM ROW(OLD.status,OLD.config_json,OLD.fingerprint) THEN
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
