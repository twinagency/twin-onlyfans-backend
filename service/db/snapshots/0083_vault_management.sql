CREATE TABLE of_vault_sync_job (
 account_id VARCHAR NOT NULL REFERENCES of_runtime(account_id) ON DELETE RESTRICT,
 job_id VARCHAR(36) NOT NULL,
 binding_id VARCHAR NOT NULL REFERENCES of_creator_bindings(id) ON DELETE RESTRICT,
 command_key VARCHAR(64) NOT NULL,
 request_hash VARCHAR(64) NOT NULL,
 actor VARCHAR(128) NOT NULL,
 connection_revision INTEGER NOT NULL,
 owner_id BIGINT NOT NULL,
 max_pages INTEGER NOT NULL,
 status VARCHAR(16) NOT NULL,
 page_offset INTEGER NOT NULL,
 pages_read INTEGER NOT NULL,
 items_seen INTEGER NOT NULL,
 lease_token VARCHAR(36),
 lease_until TIMESTAMP WITHOUT TIME ZONE,
 last_page_hash VARCHAR(64),
 last_error VARCHAR(64),
 requested_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
 updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
 completed_at TIMESTAMP WITHOUT TIME ZONE,
 PRIMARY KEY(account_id,job_id),
 CONSTRAINT uq_of_vault_sync_command UNIQUE(account_id,command_key),
 CONSTRAINT ck_of_vault_sync_job_status CHECK(status IN ('pending','running','complete','failed','superseded')),
 CONSTRAINT ck_of_vault_sync_job_bounds CHECK(connection_revision > 0 AND owner_id > 0 AND pages_read >= 0 AND pages_read <= max_pages AND max_pages = 200 AND items_seen >= 0 AND items_seen <= 4800 AND page_offset = items_seen),
 CONSTRAINT ck_of_vault_sync_job_lease CHECK((status = 'running') = (lease_token IS NOT NULL AND lease_until IS NOT NULL))
);
CREATE UNIQUE INDEX uq_of_vault_sync_active ON of_vault_sync_job(account_id) WHERE status IN ('pending','running');
CREATE TABLE of_vault_observation (
 account_id VARCHAR NOT NULL,
 media_id BIGINT NOT NULL,
 job_id VARCHAR(36) NOT NULL,
 raw_hash VARCHAR(64) NOT NULL,
 observed_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
 PRIMARY KEY(account_id,media_id),
 FOREIGN KEY(account_id,media_id) REFERENCES vault_items(account_id,media_id) ON DELETE RESTRICT,
 FOREIGN KEY(account_id,job_id) REFERENCES of_vault_sync_job(account_id,job_id) ON DELETE RESTRICT
);
CREATE TABLE of_vault_product_create (
 account_id VARCHAR NOT NULL REFERENCES of_runtime(account_id) ON DELETE RESTRICT,
 command_key VARCHAR(64) NOT NULL,
 item_id INTEGER NOT NULL REFERENCES catalog_items(id) ON DELETE RESTRICT,
 request_hash VARCHAR(64) NOT NULL,
 media_hash VARCHAR(64) NOT NULL,
 actor VARCHAR(128) NOT NULL,
 product_json TEXT NOT NULL,
 acknowledgement_json TEXT NOT NULL,
 created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
 PRIMARY KEY(account_id,command_key),
 CONSTRAINT uq_of_vault_product_media UNIQUE(account_id,media_hash)
);
CREATE FUNCTION of_vault_product_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 RAISE EXCEPTION 'vault product evidence is append-only';
END;
$$;
CREATE TRIGGER of_vault_product_immutable BEFORE UPDATE OR DELETE ON of_vault_product_create
FOR EACH ROW EXECUTE FUNCTION of_vault_product_immutable();
CREATE FUNCTION of_vault_sync_frozen() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'vault sync input is immutable'; END IF;
 IF ROW(NEW.account_id,NEW.job_id,NEW.binding_id,NEW.command_key,NEW.request_hash,NEW.actor,NEW.connection_revision,NEW.owner_id,NEW.max_pages,NEW.requested_at)
 IS DISTINCT FROM ROW(OLD.account_id,OLD.job_id,OLD.binding_id,OLD.command_key,OLD.request_hash,OLD.actor,OLD.connection_revision,OLD.owner_id,OLD.max_pages,OLD.requested_at)
 THEN RAISE EXCEPTION 'vault sync input is immutable'; END IF;
 RETURN NEW;
END;
$$;
CREATE TRIGGER of_vault_sync_frozen BEFORE UPDATE OR DELETE ON of_vault_sync_job
FOR EACH ROW EXECUTE FUNCTION of_vault_sync_frozen();
