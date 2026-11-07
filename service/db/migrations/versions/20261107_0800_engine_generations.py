"""Append-only decision generations: proved-unsent work can be regenerated."""
from alembic import op
import sqlalchemy as sa

revision = '0120_engine_generations'
down_revision = '0119_identity_independence'
branch_labels = depends_on = None


OLD_GUARD = "CREATE OR REPLACE FUNCTION of_engine_decision_guard() RETURNS trigger LANGUAGE plpgsql AS $$\n    BEGIN\n      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'engine decision evidence is retained'; END IF;\n      IF ROW(NEW.account_id,NEW.decision_id,NEW.request_id,NEW.part_index,NEW.fan_id,NEW.binding_id,NEW.owner_epoch,NEW.connection_epoch,\n             NEW.input_revision,NEW.control_revision,NEW.action,NEW.offer_grant_id,NEW.catalog_item_id,NEW.terms_json,NEW.body,NEW.payload_hash,NEW.snapshot_hash,NEW.created_at)\n         IS DISTINCT FROM ROW(OLD.account_id,OLD.decision_id,OLD.request_id,OLD.part_index,OLD.fan_id,OLD.binding_id,OLD.owner_epoch,OLD.connection_epoch,\n             OLD.input_revision,OLD.control_revision,OLD.action,OLD.offer_grant_id,OLD.catalog_item_id,OLD.terms_json,OLD.body,OLD.payload_hash,OLD.snapshot_hash,OLD.created_at)\n         OR (OLD.intent_id IS NOT NULL AND NEW.intent_id IS DISTINCT FROM OLD.intent_id)\n         OR (OLD.status = 'completed' AND NEW.status <> 'completed')\n         OR (OLD.status = 'unknown' AND NEW.status NOT IN ('unknown','completed','held'))\n      THEN RAISE EXCEPTION 'engine decision identity is immutable'; END IF;\n      RETURN NEW;\n    END $$"
NEW_GUARD = "CREATE OR REPLACE FUNCTION of_engine_decision_guard() RETURNS trigger LANGUAGE plpgsql AS $$\n    BEGIN\n      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'engine decision evidence is retained'; END IF;\n      IF ROW(NEW.account_id,NEW.decision_id,NEW.request_id,NEW.generation,NEW.part_index,NEW.fan_id,NEW.binding_id,NEW.owner_epoch,NEW.connection_epoch,\n             NEW.input_revision,NEW.control_revision,NEW.action,NEW.offer_grant_id,NEW.catalog_item_id,NEW.terms_json,NEW.body,NEW.payload_hash,NEW.snapshot_hash,NEW.created_at)\n         IS DISTINCT FROM ROW(OLD.account_id,OLD.decision_id,OLD.request_id,OLD.generation,OLD.part_index,OLD.fan_id,OLD.binding_id,OLD.owner_epoch,OLD.connection_epoch,\n             OLD.input_revision,OLD.control_revision,OLD.action,OLD.offer_grant_id,OLD.catalog_item_id,OLD.terms_json,OLD.body,OLD.payload_hash,OLD.snapshot_hash,OLD.created_at)\n         OR (OLD.intent_id IS NOT NULL AND NEW.intent_id IS DISTINCT FROM OLD.intent_id)\n         OR (OLD.status = 'completed' AND NEW.status <> 'completed')\n         OR (OLD.status = 'unknown' AND NEW.status NOT IN ('unknown','completed','held'))\n      THEN RAISE EXCEPTION 'engine decision identity is immutable'; END IF;\n      RETURN NEW;\n    END $$"


def upgrade():
    op.execute("ALTER TABLE of_engine_decision ADD COLUMN generation INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE of_engine_decision ADD CONSTRAINT ck_of_engine_decision_generation CHECK(generation >= 0)")
    op.execute("ALTER TABLE of_engine_decision DROP CONSTRAINT uq_engine_decision_request_part")
    op.execute("ALTER TABLE of_engine_decision ADD CONSTRAINT uq_engine_decision_request_generation_part UNIQUE(account_id,request_id,generation,part_index)")
    op.execute(NEW_GUARD)


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM of_engine_decision WHERE generation <> 0 LIMIT 1')).first():
        raise RuntimeError('Preserve regenerated decision evidence; use a compatible rollback')
    op.execute('ALTER TABLE of_engine_decision DROP CONSTRAINT uq_engine_decision_request_generation_part')
    op.execute('ALTER TABLE of_engine_decision ADD CONSTRAINT uq_engine_decision_request_part UNIQUE(account_id,request_id,part_index)')
    op.execute(OLD_GUARD)
    op.execute('ALTER TABLE of_engine_decision DROP CONSTRAINT ck_of_engine_decision_generation')
    op.execute('ALTER TABLE of_engine_decision DROP COLUMN generation')
