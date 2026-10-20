-- Additive native media admission. Existing media and financial owners stay unchanged.
CREATE TABLE of_vault_media_job (
    account_id VARCHAR NOT NULL,
    job_id VARCHAR(36) NOT NULL,
    binding_id VARCHAR NOT NULL,
    command_key VARCHAR(64) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    actor VARCHAR(128) NOT NULL,
    kind VARCHAR(8) NOT NULL,
    connection_revision INTEGER NOT NULL,
    owner_id BIGINT NOT NULL,
    request_json TEXT NOT NULL,
    status VARCHAR(16) NOT NULL,
    run_id VARCHAR(160),
    lease_token VARCHAR(36),
    lease_until TIMESTAMP WITHOUT TIME ZONE,
    last_error VARCHAR(64),
    cleanup_required BOOLEAN DEFAULT false NOT NULL,
    requested_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    completed_at TIMESTAMP WITHOUT TIME ZONE,
    CONSTRAINT pk_of_vault_media_job PRIMARY KEY (account_id,job_id),
    CONSTRAINT uq_of_vault_media_command UNIQUE (account_id,command_key),
    CONSTRAINT ck_of_vault_media_job_kind CHECK (kind IN ('upload','video')),
    CONSTRAINT ck_of_vault_media_job_status CHECK (status IN ('receiving','pending','running','complete','failed','interrupted','superseded','expired')),
    CONSTRAINT ck_of_vault_media_job_scope CHECK (connection_revision > 0 AND owner_id > 0),
    CONSTRAINT ck_of_vault_media_job_lease CHECK ((status = 'running') = (lease_token IS NOT NULL AND lease_until IS NOT NULL)),
    CONSTRAINT fk_of_vault_media_job_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime(account_id) ON DELETE RESTRICT,
    CONSTRAINT fk_of_vault_media_job_binding_id_of_creator_bindings FOREIGN KEY(binding_id) REFERENCES of_creator_bindings(id) ON DELETE RESTRICT
);
CREATE INDEX ix_of_vault_media_account_recent ON of_vault_media_job(account_id,kind,requested_at);
CREATE INDEX ix_of_vault_media_binding ON of_vault_media_job(binding_id);
CREATE INDEX ix_of_vault_media_cleanup ON of_vault_media_job(updated_at) WHERE cleanup_required;
CREATE INDEX ix_of_vault_media_pending ON of_vault_media_job(kind,status,requested_at);
CREATE UNIQUE INDEX uq_of_vault_media_active ON of_vault_media_job(account_id,kind) WHERE status IN ('receiving','pending','running');

CREATE OR REPLACE FUNCTION of_vault_media_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'Retain native media admission and recovery evidence';
  END IF;
  IF ROW(NEW.account_id,NEW.job_id,NEW.binding_id,NEW.command_key,NEW.request_hash,NEW.actor,NEW.kind,NEW.connection_revision,NEW.owner_id,NEW.request_json,NEW.requested_at)
     IS DISTINCT FROM ROW(OLD.account_id,OLD.job_id,OLD.binding_id,OLD.command_key,OLD.request_hash,OLD.actor,OLD.kind,OLD.connection_revision,OLD.owner_id,OLD.request_json,OLD.requested_at)
     OR (OLD.run_id IS NOT NULL AND NEW.run_id IS DISTINCT FROM OLD.run_id) THEN
    RAISE EXCEPTION 'Native media admission is immutable';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER of_vault_media_guard BEFORE UPDATE OR DELETE ON of_vault_media_job
FOR EACH ROW EXECUTE FUNCTION of_vault_media_guard();
