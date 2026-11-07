"""Supervised-pilot approval gate: one immutable row per approval version of a draft reply/offer sequence.

Chain: ...0122_fansapi_transport -> 0123_engine_model_calls -> 0124_engine_approval (this file).
A row binds the exact ordered parts, offer terms and conversation state (draft_hash). Content never changes after
insert; only status/decision fields move forward. Terminal rows are frozen; nothing is ever deleted.
"""
from alembic import op
import sqlalchemy as sa

revision = '0124_engine_approval'
down_revision = '0123_engine_model_calls'
branch_labels = depends_on = None

TABLE = """CREATE TABLE of_engine_approval (
  account_id VARCHAR NOT NULL, request_id VARCHAR(36) NOT NULL, generation INTEGER NOT NULL, version INTEGER NOT NULL,
  fan_id BIGINT NOT NULL, binding_id VARCHAR NOT NULL, source_message_id BIGINT NOT NULL,
  owner_epoch BIGINT NOT NULL, control_revision INTEGER NOT NULL, input_revision BIGINT NOT NULL, connection_epoch BIGINT,
  status VARCHAR(16) NOT NULL, parts_json TEXT NOT NULL, draft_hash VARCHAR(64) NOT NULL,
  created_by VARCHAR(128) NOT NULL, actor VARCHAR(128), decided_at TIMESTAMP WITHOUT TIME ZONE,
  expires_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, invalidation_reason VARCHAR(64),
  created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
  CONSTRAINT pk_of_engine_approval PRIMARY KEY(account_id, request_id, generation, version),
  CONSTRAINT fk_engine_approval_request FOREIGN KEY(account_id, request_id) REFERENCES of_engine_request(account_id, request_id) ON DELETE RESTRICT,
  CONSTRAINT fk_engine_approval_binding FOREIGN KEY(binding_id) REFERENCES of_creator_bindings(id) ON DELETE RESTRICT,
  CONSTRAINT ck_of_engine_approval_status CHECK(status IN ('awaiting','approved','rejected','invalidated','expired','consumed')),
  CONSTRAINT ck_of_engine_approval_numbers CHECK(version >= 1 AND generation >= 0 AND owner_epoch > 0 AND control_revision >= 0),
  CONSTRAINT ck_of_engine_approval_decided CHECK(status NOT IN ('approved','consumed') OR (actor IS NOT NULL AND decided_at IS NOT NULL)),
  CONSTRAINT ck_of_engine_approval_reason CHECK(status NOT IN ('rejected','invalidated','expired') OR invalidation_reason IS NOT NULL)
)"""

INDEXES = [
    "CREATE UNIQUE INDEX uq_engine_approval_live ON of_engine_approval(account_id, request_id, generation) WHERE status IN ('awaiting','approved')",
    'CREATE INDEX ix_engine_approval_fan ON of_engine_approval(account_id, fan_id, status)',
]

GUARD = """CREATE FUNCTION of_engine_approval_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'engine approval evidence is retained'; END IF;
  IF TG_OP = 'INSERT' THEN
    IF NEW.status <> 'awaiting' OR NEW.actor IS NOT NULL OR NEW.decided_at IS NOT NULL THEN
      RAISE EXCEPTION 'engine approval starts awaiting a decision'; END IF;
    RETURN NEW;
  END IF;
  IF ROW(NEW.account_id,NEW.request_id,NEW.generation,NEW.version,NEW.fan_id,NEW.binding_id,NEW.source_message_id,NEW.owner_epoch,
         NEW.control_revision,NEW.input_revision,NEW.connection_epoch,NEW.parts_json,NEW.draft_hash,NEW.created_by,NEW.created_at)
     IS DISTINCT FROM ROW(OLD.account_id,OLD.request_id,OLD.generation,OLD.version,OLD.fan_id,OLD.binding_id,OLD.source_message_id,OLD.owner_epoch,
         OLD.control_revision,OLD.input_revision,OLD.connection_epoch,OLD.parts_json,OLD.draft_hash,OLD.created_by,OLD.created_at)
     OR OLD.status IN ('rejected','invalidated','expired','consumed')
     OR (OLD.status = 'awaiting' AND NEW.status NOT IN ('awaiting','approved','rejected','invalidated','expired'))
     OR (OLD.status = 'approved' AND NEW.status NOT IN ('approved','consumed','invalidated','expired'))
     OR (OLD.status = 'approved' AND ROW(NEW.actor,NEW.decided_at) IS DISTINCT FROM ROW(OLD.actor,OLD.decided_at) AND NEW.status NOT IN ('invalidated','expired'))
  THEN RAISE EXCEPTION 'engine approval version is immutable'; END IF;
  RETURN NEW;
END $$"""


def upgrade():
    op.execute(TABLE)
    for statement in INDEXES:
        op.execute(statement)
    op.execute(GUARD)
    op.execute('CREATE TRIGGER of_engine_approval_guard BEFORE INSERT OR UPDATE OR DELETE ON of_engine_approval FOR EACH ROW EXECUTE FUNCTION of_engine_approval_guard()')
    op.execute('ALTER TABLE of_engine_approval ENABLE ROW LEVEL SECURITY')
    op.execute('REVOKE ALL ON of_engine_approval FROM PUBLIC')
    for role in ('anon', 'authenticated'):
        op.execute(f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{role}') THEN REVOKE ALL ON of_engine_approval FROM {role}; END IF; END $$")


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM of_engine_approval LIMIT 1')).first():
        raise RuntimeError('Preserve engine approval evidence; use a compatible rollback')
    op.execute('DROP TRIGGER of_engine_approval_guard ON of_engine_approval')
    op.execute('DROP FUNCTION of_engine_approval_guard()')
    op.drop_table('of_engine_approval')
