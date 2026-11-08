"""Immutable, uniquely linked offer authorization issuances; no old authority is changed."""
from alembic import op
from sqlalchemy import text
revision = '0127_offer_grant_lineage'
down_revision = '0126_plan_c_ingress'
branch_labels = depends_on = None
GUARD = "CREATE OR REPLACE FUNCTION of_engine_offer_grant_guard() RETURNS trigger LANGUAGE plpgsql AS $$\nDECLARE predecessor of_engine_offer_grant%ROWTYPE;\nBEGIN\n  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'offer authorization evidence is retained'; END IF;\n  IF TG_OP = 'INSERT' THEN\n    IF NEW.predecessor_grant_id IS NOT NULL THEN\n      SELECT * INTO predecessor FROM of_engine_offer_grant WHERE account_id=NEW.account_id AND grant_id=NEW.predecessor_grant_id;\n      IF NOT FOUND OR ROW(NEW.fan_id,NEW.binding_id,NEW.item_id,NEW.terms_hash,NEW.issuance)\n         IS DISTINCT FROM ROW(predecessor.fan_id,predecessor.binding_id,predecessor.item_id,predecessor.terms_hash,predecessor.issuance+1)\n      THEN RAISE EXCEPTION 'offer authorization predecessor mismatch'; END IF;\n    END IF;\n    RETURN NEW;\n  END IF;\n  IF ROW(NEW.account_id,NEW.grant_id,NEW.fan_id,NEW.binding_id,NEW.item_id,NEW.terms_hash,NEW.coverage_ref,NEW.expires_at,NEW.created_at,NEW.issuance,NEW.predecessor_grant_id,NEW.issuance_reason,NEW.authorization_json)\n     IS DISTINCT FROM ROW(OLD.account_id,OLD.grant_id,OLD.fan_id,OLD.binding_id,OLD.item_id,OLD.terms_hash,OLD.coverage_ref,OLD.expires_at,OLD.created_at,OLD.issuance,OLD.predecessor_grant_id,OLD.issuance_reason,OLD.authorization_json)\n     OR (OLD.used_intent_id IS NOT NULL AND NEW.used_intent_id IS DISTINCT FROM OLD.used_intent_id)\n  THEN RAISE EXCEPTION 'offer authorization identity is immutable'; END IF;\n  RETURN NEW;\nEND $$"
OLD_GUARD = "CREATE FUNCTION of_engine_offer_grant_guard() RETURNS trigger LANGUAGE plpgsql AS $$\n    BEGIN\n      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'offer authorization evidence is retained'; END IF;\n      IF ROW(NEW.account_id,NEW.grant_id,NEW.fan_id,NEW.binding_id,NEW.item_id,NEW.terms_hash,NEW.coverage_ref,NEW.expires_at,NEW.created_at)\n         IS DISTINCT FROM ROW(OLD.account_id,OLD.grant_id,OLD.fan_id,OLD.binding_id,OLD.item_id,OLD.terms_hash,OLD.coverage_ref,OLD.expires_at,OLD.created_at)\n         OR (OLD.used_intent_id IS NOT NULL AND NEW.used_intent_id IS DISTINCT FROM OLD.used_intent_id)\n      THEN RAISE EXCEPTION 'offer authorization identity is immutable'; END IF;\n      RETURN NEW;\n    END $$"

def upgrade():
    op.execute("ALTER TABLE of_engine_offer_grant ADD COLUMN issuance integer NOT NULL DEFAULT 0, ADD COLUMN predecessor_grant_id varchar(36), ADD COLUMN issuance_reason varchar(32) NOT NULL DEFAULT 'initial', ADD COLUMN authorization_json text NOT NULL DEFAULT '{\"kind\":\"legacy_initial\"}'")
    op.execute('ALTER TABLE of_engine_offer_grant DROP CONSTRAINT uq_engine_offer_grant_terms')
    op.execute('ALTER TABLE of_engine_offer_grant ADD CONSTRAINT uq_engine_offer_grant_issuance UNIQUE(account_id,fan_id,item_id,terms_hash,issuance), ADD CONSTRAINT uq_engine_offer_grant_successor UNIQUE(account_id,predecessor_grant_id), ADD CONSTRAINT fk_engine_offer_grant_predecessor FOREIGN KEY(account_id,predecessor_grant_id) REFERENCES of_engine_offer_grant(account_id,grant_id) ON DELETE RESTRICT')
    op.execute("ALTER TABLE of_engine_offer_grant ADD CONSTRAINT ck_of_engine_offer_grant_lineage CHECK (issuance >= 0 AND ((issuance = 0 AND predecessor_grant_id IS NULL AND issuance_reason = 'initial') OR (issuance > 0 AND predecessor_grant_id IS NOT NULL AND issuance_reason IN ('cancelled_unsent','expired_renewal')))), ADD CONSTRAINT ck_of_engine_offer_grant_authorization CHECK (jsonb_typeof(authorization_json::jsonb) = 'object')")
    op.execute('DROP TRIGGER of_engine_offer_grant_guard ON of_engine_offer_grant')
    op.execute(GUARD)
    op.execute('CREATE TRIGGER of_engine_offer_grant_guard BEFORE INSERT OR UPDATE OR DELETE ON of_engine_offer_grant FOR EACH ROW EXECUTE FUNCTION of_engine_offer_grant_guard()')

def downgrade():
    if op.get_bind().scalar(text("SELECT EXISTS(SELECT 1 FROM of_engine_offer_grant WHERE issuance > 0 OR authorization_json::jsonb <> '{\"kind\":\"legacy_initial\"}'::jsonb)")):
        raise RuntimeError('offer issuance evidence prevents downgrade')
    op.execute('DROP TRIGGER of_engine_offer_grant_guard ON of_engine_offer_grant')
    op.execute('DROP FUNCTION of_engine_offer_grant_guard()')
    op.execute(OLD_GUARD)
    op.execute('CREATE TRIGGER of_engine_offer_grant_guard BEFORE UPDATE OR DELETE ON of_engine_offer_grant FOR EACH ROW EXECUTE FUNCTION of_engine_offer_grant_guard()')
    op.execute('ALTER TABLE of_engine_offer_grant DROP CONSTRAINT fk_engine_offer_grant_predecessor, DROP CONSTRAINT uq_engine_offer_grant_successor, DROP CONSTRAINT uq_engine_offer_grant_issuance, DROP CONSTRAINT ck_of_engine_offer_grant_lineage, DROP CONSTRAINT ck_of_engine_offer_grant_authorization')
    op.execute('ALTER TABLE of_engine_offer_grant DROP COLUMN issuance, DROP COLUMN predecessor_grant_id, DROP COLUMN issuance_reason, DROP COLUMN authorization_json, ADD CONSTRAINT uq_engine_offer_grant_terms UNIQUE(account_id,fan_id,item_id,terms_hash)')
