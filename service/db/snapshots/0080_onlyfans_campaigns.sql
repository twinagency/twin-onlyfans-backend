-- New campaign definitions never activate runtime, delivery or a provider.
ALTER TABLE of_text_intent DROP CONSTRAINT ck_of_text_intent_origin;
ALTER TABLE of_text_intent ADD CONSTRAINT ck_of_text_intent_origin CHECK (origin IN ('automated','manual','campaign'));

CREATE TABLE of_campaign (
 account_id VARCHAR NOT NULL REFERENCES of_runtime(account_id) ON DELETE RESTRICT,
 campaign_id VARCHAR(36) NOT NULL,
 binding_id VARCHAR NOT NULL REFERENCES of_creator_bindings(id) ON DELETE RESTRICT,
 command_key VARCHAR(64) NOT NULL,
 command_hash VARCHAR(64) NOT NULL,
 actor VARCHAR(128) NOT NULL,
 name VARCHAR(128) NOT NULL,
 body TEXT NOT NULL,
 catalog_item_id INTEGER REFERENCES catalog_items(id) ON DELETE RESTRICT,
 catalog_snapshot_json TEXT,
 audience_kind VARCHAR(16) NOT NULL,
 audience_hash VARCHAR(64) NOT NULL,
 recipient_count INTEGER NOT NULL,
 status VARCHAR(16) NOT NULL,
 revision INTEGER NOT NULL,
 scheduled_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
 created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
 updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
 PRIMARY KEY(account_id,campaign_id),
 CONSTRAINT uq_of_campaign_command UNIQUE(account_id,command_key),
 CONSTRAINT ck_of_campaign_status CHECK(status IN ('scheduled','running','paused','cancelled','completed')),
 CONSTRAINT ck_of_campaign_audience_kind CHECK(audience_kind IN ('explicit','all_known')),
 CONSTRAINT ck_of_campaign_recipient_count CHECK(recipient_count > 0 AND recipient_count <= 1000),
 CONSTRAINT ck_of_campaign_revision CHECK(revision > 0),
 CONSTRAINT ck_of_campaign_offer_shape CHECK((catalog_item_id IS NULL AND catalog_snapshot_json IS NULL) OR (catalog_item_id IS NOT NULL AND catalog_snapshot_json IS NOT NULL))
);
CREATE INDEX ix_of_campaign_due ON of_campaign(account_id,status,scheduled_at,campaign_id);

CREATE TABLE of_campaign_recipient (
 account_id VARCHAR NOT NULL,
 campaign_id VARCHAR(36) NOT NULL,
 fan_id BIGINT NOT NULL,
 status VARCHAR(16) NOT NULL,
 intent_id VARCHAR(36),
 reason VARCHAR(128),
 updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
 PRIMARY KEY(account_id,campaign_id,fan_id),
 FOREIGN KEY(account_id,campaign_id) REFERENCES of_campaign(account_id,campaign_id) ON DELETE RESTRICT,
 FOREIGN KEY(account_id,fan_id) REFERENCES fans(account_id,fan_id) ON DELETE RESTRICT,
 FOREIGN KEY(account_id,intent_id) REFERENCES of_text_intent(account_id,intent_id) ON DELETE RESTRICT,
 CONSTRAINT uq_of_campaign_recipient_intent UNIQUE(account_id,intent_id),
 CONSTRAINT ck_of_campaign_recipient_status CHECK(status IN ('pending','queued','skipped','cancelled'))
);
CREATE INDEX ix_of_campaign_recipient_pending ON of_campaign_recipient(account_id,campaign_id,status,fan_id);

CREATE TABLE of_campaign_change (
 account_id VARCHAR NOT NULL,
 campaign_id VARCHAR(36) NOT NULL,
 revision INTEGER NOT NULL,
 command_key VARCHAR(64) NOT NULL,
 command_hash VARCHAR(64) NOT NULL,
 action VARCHAR(16) NOT NULL,
 actor VARCHAR(128) NOT NULL,
 before_json TEXT NOT NULL,
 after_json TEXT NOT NULL,
 created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
 PRIMARY KEY(account_id,campaign_id,revision),
 FOREIGN KEY(account_id,campaign_id) REFERENCES of_campaign(account_id,campaign_id) ON DELETE RESTRICT,
 CONSTRAINT uq_of_campaign_change_command UNIQUE(account_id,command_key),
 CONSTRAINT ck_of_campaign_change_revision CHECK(revision > 0)
);
CREATE FUNCTION of_campaign_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'campaign evidence is append-only';
END;
$$;
CREATE TRIGGER of_campaign_immutable BEFORE UPDATE OR DELETE ON of_campaign_change
FOR EACH ROW EXECUTE FUNCTION of_campaign_immutable();
