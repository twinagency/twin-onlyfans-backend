"""Approved commercial catalogue terms: immutable revisions, exact members, grant lineage for terms successors.

Additive. `down_revision` is 0133 ON THIS BRANCH ONLY: 0134_commercial_scope_contract is being built on a separate branch,
so the owner re-parents this revision onto 0134 at merge (change `down_revision` and nothing else; schema.py pins key on
the 0135 rank). Legacy items keep no terms row and behave byte for byte as before.
"""
from alembic import op
from sqlalchemy import text

revision = "0135_commercial_catalogue_terms"
down_revision = "0134_commercial_scope_contract"  # integration: step 3 re-parented onto step 2
branch_labels = depends_on = None

OLD_GUARD = "CREATE OR REPLACE FUNCTION of_engine_offer_grant_guard() RETURNS trigger LANGUAGE plpgsql AS $$\nDECLARE predecessor of_engine_offer_grant%ROWTYPE;\nBEGIN\n  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'offer authorization evidence is retained'; END IF;\n  IF TG_OP = 'INSERT' THEN\n    IF NEW.predecessor_grant_id IS NOT NULL THEN\n      SELECT * INTO predecessor FROM of_engine_offer_grant WHERE account_id=NEW.account_id AND grant_id=NEW.predecessor_grant_id;\n      IF NOT FOUND OR ROW(NEW.fan_id,NEW.binding_id,NEW.item_id,NEW.terms_hash,NEW.issuance)\n         IS DISTINCT FROM ROW(predecessor.fan_id,predecessor.binding_id,predecessor.item_id,predecessor.terms_hash,predecessor.issuance+1)\n      THEN RAISE EXCEPTION 'offer authorization predecessor mismatch'; END IF;\n    END IF;\n    RETURN NEW;\n  END IF;\n  IF ROW(NEW.account_id,NEW.grant_id,NEW.fan_id,NEW.binding_id,NEW.item_id,NEW.terms_hash,NEW.coverage_ref,NEW.expires_at,NEW.created_at,NEW.issuance,NEW.predecessor_grant_id,NEW.issuance_reason,NEW.authorization_json)\n     IS DISTINCT FROM ROW(OLD.account_id,OLD.grant_id,OLD.fan_id,OLD.binding_id,OLD.item_id,OLD.terms_hash,OLD.coverage_ref,OLD.expires_at,OLD.created_at,OLD.issuance,OLD.predecessor_grant_id,OLD.issuance_reason,OLD.authorization_json)\n     OR (OLD.used_intent_id IS NOT NULL AND NEW.used_intent_id IS DISTINCT FROM OLD.used_intent_id)\n  THEN RAISE EXCEPTION 'offer authorization identity is immutable'; END IF;\n  RETURN NEW;\nEND $$"
GUARD = "CREATE OR REPLACE FUNCTION of_engine_offer_grant_guard() RETURNS trigger LANGUAGE plpgsql AS $$\nDECLARE predecessor of_engine_offer_grant%ROWTYPE;\nBEGIN\n  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'offer authorization evidence is retained'; END IF;\n  IF TG_OP = 'INSERT' THEN\n    IF NEW.predecessor_grant_id IS NOT NULL THEN\n      SELECT * INTO predecessor FROM of_engine_offer_grant WHERE account_id=NEW.account_id AND grant_id=NEW.predecessor_grant_id;\n      IF NOT FOUND OR ROW(NEW.fan_id,NEW.binding_id,NEW.item_id,NEW.issuance)\n         IS DISTINCT FROM ROW(predecessor.fan_id,predecessor.binding_id,predecessor.item_id,predecessor.issuance+1)\n         OR (NEW.issuance_reason IN ('terms_revision','discount_variant'))\n            = (NEW.terms_hash IS NOT DISTINCT FROM predecessor.terms_hash)\n      THEN RAISE EXCEPTION 'offer authorization predecessor mismatch'; END IF;\n    END IF;\n    RETURN NEW;\n  END IF;\n  IF ROW(NEW.account_id,NEW.grant_id,NEW.fan_id,NEW.binding_id,NEW.item_id,NEW.terms_hash,NEW.coverage_ref,NEW.expires_at,NEW.created_at,NEW.issuance,NEW.predecessor_grant_id,NEW.issuance_reason,NEW.authorization_json)\n     IS DISTINCT FROM ROW(OLD.account_id,OLD.grant_id,OLD.fan_id,OLD.binding_id,OLD.item_id,OLD.terms_hash,OLD.coverage_ref,OLD.expires_at,OLD.created_at,OLD.issuance,OLD.predecessor_grant_id,OLD.issuance_reason,OLD.authorization_json)\n     OR (OLD.used_intent_id IS NOT NULL AND NEW.used_intent_id IS DISTINCT FROM OLD.used_intent_id)\n  THEN RAISE EXCEPTION 'offer authorization identity is immutable'; END IF;\n  RETURN NEW;\nEND $$"
TERMS_GUARD = "CREATE OR REPLACE FUNCTION of_catalogue_terms_guard() RETURNS trigger LANGUAGE plpgsql AS $$\nBEGIN\n  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'catalogue terms evidence is retained'; END IF;\n  IF ROW(NEW.account_id,NEW.item_id,NEW.revision,NEW.product_kind,NEW.standard_price_cents,NEW.discount_price_cents,\n         NEW.price_source,NEW.category_folder_id,NEW.category_price_revision,NEW.media_count,NEW.paid_photo_count,\n         NEW.paid_video_count,NEW.preview_photo_count,NEW.preview_video_count,NEW.approved_by,NEW.approved_at,\n         NEW.binding_id,NEW.command_key,NEW.request_hash,NEW.snapshot_json,NEW.snapshot_sha256)\n     IS DISTINCT FROM ROW(OLD.account_id,OLD.item_id,OLD.revision,OLD.product_kind,OLD.standard_price_cents,OLD.discount_price_cents,\n         OLD.price_source,OLD.category_folder_id,OLD.category_price_revision,OLD.media_count,OLD.paid_photo_count,\n         OLD.paid_video_count,OLD.preview_photo_count,OLD.preview_video_count,OLD.approved_by,OLD.approved_at,\n         OLD.binding_id,OLD.command_key,OLD.request_hash,OLD.snapshot_json,OLD.snapshot_sha256)\n     OR NOT (OLD.status = 'approved' AND NEW.status IN ('approved','superseded','withdrawn'))\n  THEN RAISE EXCEPTION 'catalogue terms are immutable'; END IF;\n  RETURN NEW;\nEND $$"
MEMBER_GUARD = "CREATE OR REPLACE FUNCTION of_catalogue_terms_member_guard() RETURNS trigger LANGUAGE plpgsql AS $$\nBEGIN\n  RAISE EXCEPTION 'catalogue terms members are immutable';\nEND $$"

OLD_LINEAGE = ("issuance >= 0 AND ((issuance = 0 AND predecessor_grant_id IS NULL AND issuance_reason = 'initial') OR "
               "(issuance > 0 AND predecessor_grant_id IS NOT NULL AND issuance_reason IN ('cancelled_unsent','expired_renewal')))")
NEW_LINEAGE = ("issuance >= 0 AND ((issuance = 0 AND predecessor_grant_id IS NULL AND issuance_reason = 'initial') OR "
               "(issuance > 0 AND predecessor_grant_id IS NOT NULL AND issuance_reason IN "
               "('cancelled_unsent','expired_renewal','terms_revision','discount_variant')))")


def upgrade():
    op.execute("""
CREATE TABLE of_catalogue_terms (
  account_id varchar NOT NULL, item_id integer NOT NULL, revision integer NOT NULL,
  status varchar(12) NOT NULL, product_kind varchar(16) NOT NULL,
  standard_price_cents integer NOT NULL, discount_price_cents integer,
  price_source varchar(24) NOT NULL, category_folder_id bigint, category_price_revision integer,
  media_count integer NOT NULL, paid_photo_count integer NOT NULL, paid_video_count integer NOT NULL,
  preview_photo_count integer NOT NULL, preview_video_count integer NOT NULL,
  approved_by varchar(128) NOT NULL, approved_at timestamp NOT NULL, binding_id varchar NOT NULL,
  command_key varchar(64) NOT NULL, request_hash varchar(64) NOT NULL, snapshot_json text NOT NULL, snapshot_sha256 varchar(64) NOT NULL,
  CONSTRAINT pk_of_catalogue_terms PRIMARY KEY (account_id, item_id, revision),
  CONSTRAINT fk_of_catalogue_terms_account_id_of_runtime FOREIGN KEY (account_id) REFERENCES of_runtime(account_id) ON DELETE RESTRICT,
  CONSTRAINT fk_of_catalogue_terms_item_id_catalog_items FOREIGN KEY (item_id) REFERENCES catalog_items(id) ON DELETE RESTRICT,
  CONSTRAINT fk_of_catalogue_terms_binding_id_of_creator_bindings FOREIGN KEY (binding_id) REFERENCES of_creator_bindings(id) ON DELETE RESTRICT,
  CONSTRAINT uq_of_catalogue_terms_command UNIQUE (account_id, command_key),
  CONSTRAINT ck_of_catalogue_terms_status CHECK (status IN ('approved','superseded','withdrawn')),
  CONSTRAINT ck_of_catalogue_terms_kind CHECK (product_kind IN ('single_photo','single_video','bundle_photo','bundle_video','bundle_mixed')),
  CONSTRAINT ck_of_catalogue_terms_standard CHECK (revision >= 1 AND standard_price_cents BETWEEN 300 AND 20000),
  CONSTRAINT ck_of_catalogue_terms_discount CHECK (discount_price_cents IS NULL OR (discount_price_cents >= 300 AND discount_price_cents < standard_price_cents)),
  CONSTRAINT ck_of_catalogue_terms_source CHECK (price_source IN ('agency_category','agency_bundle_total')),
  CONSTRAINT ck_of_catalogue_terms_source_kind CHECK (
    (product_kind IN ('single_photo','single_video') AND price_source = 'agency_category' AND category_folder_id IS NOT NULL
       AND category_price_revision IS NOT NULL AND category_price_revision > 0)
    OR (product_kind IN ('bundle_photo','bundle_video','bundle_mixed') AND price_source = 'agency_bundle_total')),
  CONSTRAINT ck_of_catalogue_terms_counts CHECK (paid_photo_count >= 0 AND paid_video_count >= 0 AND preview_photo_count >= 0
    AND preview_video_count >= 0 AND media_count = paid_photo_count + paid_video_count + preview_photo_count + preview_video_count),
  CONSTRAINT ck_of_catalogue_terms_shape CHECK (
    (product_kind = 'single_photo' AND paid_photo_count = 1 AND paid_video_count = 0)
    OR (product_kind = 'single_video' AND paid_photo_count = 0 AND paid_video_count = 1)
    OR (product_kind = 'bundle_photo' AND paid_photo_count >= 2 AND paid_video_count = 0)
    OR (product_kind = 'bundle_video' AND paid_photo_count = 0 AND paid_video_count >= 2)
    OR (product_kind = 'bundle_mixed' AND paid_photo_count >= 1 AND paid_video_count >= 1 AND paid_photo_count + paid_video_count >= 2)))""")
    op.execute("CREATE UNIQUE INDEX uq_of_catalogue_terms_live ON of_catalogue_terms (account_id, item_id) WHERE status = 'approved'")
    op.execute("CREATE INDEX ix_of_catalogue_terms_kind ON of_catalogue_terms (account_id, product_kind, item_id) WHERE status = 'approved'")
    op.execute("""
CREATE TABLE of_catalogue_terms_member (
  account_id varchar NOT NULL, item_id integer NOT NULL, revision integer NOT NULL, media_id bigint NOT NULL,
  role varchar(8) NOT NULL, kind varchar(8) NOT NULL,
  CONSTRAINT pk_of_catalogue_terms_member PRIMARY KEY (account_id, item_id, revision, media_id),
  CONSTRAINT fk_of_catalogue_terms_member_account_id_of_catalogue_terms FOREIGN KEY (account_id, item_id, revision)
    REFERENCES of_catalogue_terms(account_id, item_id, revision) ON DELETE RESTRICT,
  CONSTRAINT ck_of_catalogue_terms_member_role CHECK (role IN ('paid','preview')),
  CONSTRAINT ck_of_catalogue_terms_member_kind CHECK (kind IN ('photo','video')))""")
    op.execute("CREATE INDEX ix_terms_member_media ON of_catalogue_terms_member (account_id, media_id)")
    # Full-text candidate lookup without a table rewrite: an expression index on the existing columns.
    op.execute("CREATE INDEX ix_catalog_items_terms_search ON catalog_items USING gin ("
               "to_tsvector('simple', coalesce(label, '') || ' ' || coalesce(description_for_ai, '') || ' ' || coalesce(tags, '')))")
    op.execute(TERMS_GUARD)
    op.execute("CREATE TRIGGER of_catalogue_terms_guard BEFORE UPDATE OR DELETE ON of_catalogue_terms FOR EACH ROW EXECUTE FUNCTION of_catalogue_terms_guard()")
    op.execute(MEMBER_GUARD)
    op.execute("CREATE TRIGGER of_catalogue_terms_member_guard BEFORE UPDATE OR DELETE ON of_catalogue_terms_member FOR EACH ROW EXECUTE FUNCTION of_catalogue_terms_member_guard()")
    # Lineage: a terms successor keeps fan/binding/item and issuance+1 but must carry a different terms hash.
    op.execute("ALTER TABLE of_engine_offer_grant DROP CONSTRAINT ck_of_engine_offer_grant_lineage")
    op.execute("ALTER TABLE of_engine_offer_grant ADD CONSTRAINT ck_of_engine_offer_grant_lineage CHECK (" + NEW_LINEAGE + ")")
    op.execute(GUARD)


def downgrade():
    bind = op.get_bind()
    if bind.scalar(text("SELECT EXISTS(SELECT 1 FROM of_catalogue_terms)")):
        raise RuntimeError("Preserve approved catalogue terms evidence; use compatible code rollback")
    if bind.scalar(text("SELECT EXISTS(SELECT 1 FROM of_engine_offer_grant WHERE issuance_reason IN ('terms_revision','discount_variant'))")):
        raise RuntimeError("Preserve terms successor grant lineage; use compatible code rollback")
    op.execute(OLD_GUARD)
    op.execute("ALTER TABLE of_engine_offer_grant DROP CONSTRAINT ck_of_engine_offer_grant_lineage")
    op.execute("ALTER TABLE of_engine_offer_grant ADD CONSTRAINT ck_of_engine_offer_grant_lineage CHECK (" + OLD_LINEAGE + ")")
    op.execute("DROP INDEX ix_catalog_items_terms_search")
    op.execute("DROP TRIGGER of_catalogue_terms_member_guard ON of_catalogue_terms_member")
    op.execute("DROP FUNCTION of_catalogue_terms_member_guard()")
    op.execute("DROP TABLE of_catalogue_terms_member")
    op.execute("DROP TRIGGER of_catalogue_terms_guard ON of_catalogue_terms")
    op.execute("DROP FUNCTION of_catalogue_terms_guard()")
    op.execute("DROP TABLE of_catalogue_terms")
