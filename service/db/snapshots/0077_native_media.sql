-- Existing text rows retain their exact payload, spend, holds and receipts.
-- A separate explicit policy revision is required to permit any media.
ALTER TABLE of_delivery_policy ADD COLUMN media_enabled BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE of_native_plan ADD COLUMN sale_json TEXT;
ALTER TABLE of_text_intent ADD COLUMN media_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE of_text_intent ADD COLUMN preview_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE of_text_intent ADD COLUMN price_cents INTEGER NOT NULL DEFAULT 0;
ALTER TABLE of_text_intent ADD COLUMN catalog_item_id INTEGER REFERENCES catalog_items(id) ON DELETE RESTRICT;
ALTER TABLE of_text_intent ADD COLUMN catalog_snapshot_json TEXT;
ALTER TABLE of_text_intent ADD COLUMN offer_id INTEGER REFERENCES content_offers(id) ON DELETE RESTRICT;
ALTER TABLE of_text_intent ADD CONSTRAINT ck_of_text_intent_media_shape CHECK (
    (catalog_item_id IS NULL AND media_json = '[]' AND preview_json = '[]' AND price_cents = 0 AND catalog_snapshot_json IS NULL AND offer_id IS NULL)
    OR (catalog_item_id IS NOT NULL AND media_json <> '[]' AND catalog_snapshot_json IS NOT NULL AND price_cents >= 300 AND price_cents <= 20000)
);
-- An accepted immutable intent owns at most one canonical offer.
CREATE UNIQUE INDEX uq_of_text_intent_offer ON of_text_intent(offer_id) WHERE offer_id IS NOT NULL;
CREATE TABLE of_commerce_evidence (
    account_id VARCHAR NOT NULL,
    evidence_hash VARCHAR(64) NOT NULL,
    event_key VARCHAR(64) NOT NULL,
    provider_transaction_id VARCHAR(64) NOT NULL,
    provider_revision BIGINT NOT NULL,
    fan_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    intent_id VARCHAR(36) NOT NULL,
    kind VARCHAR(16) NOT NULL,
    status VARCHAR(16) NOT NULL,
    transaction_id INTEGER REFERENCES transactions(id) ON DELETE RESTRICT,
    occurred_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    recorded_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    evidence_ref VARCHAR(256) NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(account_id, evidence_hash),
    FOREIGN KEY(account_id,intent_id) REFERENCES of_text_intent(account_id,intent_id) ON DELETE RESTRICT,
    FOREIGN KEY(account_id,fan_id,message_id) REFERENCES messages(account_id,fan_id,message_id) ON DELETE RESTRICT,
    CONSTRAINT ck_of_commerce_evidence_kind CHECK(kind IN ('purchase','refund','chargeback')),
    CONSTRAINT ck_of_commerce_evidence_status CHECK(status IN ('applied','stale','conflict')),
    CONSTRAINT ck_of_commerce_evidence_provider_revision CHECK(provider_revision > 0)
);
CREATE INDEX ix_of_commerce_event ON of_commerce_evidence(account_id,event_key);
CREATE INDEX ix_of_commerce_revision ON of_commerce_evidence(account_id,provider_transaction_id,provider_revision);
CREATE INDEX ix_of_commerce_conflict ON of_commerce_evidence(account_id,status) WHERE status = 'conflict';
CREATE FUNCTION of_commerce_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'commerce evidence is append-only';
END;
$$;
CREATE TRIGGER of_commerce_immutable BEFORE UPDATE OR DELETE ON of_commerce_evidence
FOR EACH ROW EXECUTE FUNCTION of_commerce_immutable();
