-- Additive paid-offer import evidence and fixed catalogue pricing.
-- Existing catalogue rows become dynamic, keep their price and stay as enabled as they were.
ALTER TABLE catalog_items ADD COLUMN pricing_mode VARCHAR DEFAULT 'dynamic' NOT NULL;
ALTER TABLE catalog_items ADD CONSTRAINT ck_catalog_items_pricing_mode CHECK (pricing_mode IN ('dynamic','fixed'));

CREATE TABLE of_offer_observation (
    account_id VARCHAR NOT NULL,
    observation_id VARCHAR(36) NOT NULL,
    binding_id VARCHAR NOT NULL,
    source_kind VARCHAR(8) NOT NULL,
    source_id BIGINT NOT NULL,
    fan_id BIGINT,
    source_context VARCHAR(8) NOT NULL,
    source_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    observed_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    media_json TEXT NOT NULL,
    preview_json TEXT,
    locked_text BOOLEAN,
    price_cents INTEGER NOT NULL,
    currency VARCHAR(3) NOT NULL,
    parser_version VARCHAR(16) NOT NULL,
    fingerprint VARCHAR(64) NOT NULL,
    media_key VARCHAR(64) NOT NULL,
    reasons_json TEXT NOT NULL,
    CONSTRAINT pk_of_offer_observation PRIMARY KEY (account_id,observation_id),
    CONSTRAINT uq_of_offer_observation_source UNIQUE (account_id,binding_id,source_kind,source_id,fingerprint),
    CONSTRAINT ck_of_offer_observation_kind CHECK (source_kind IN ('message','post')),
    CONSTRAINT ck_of_offer_observation_context CHECK (source_context IN ('direct','queue','unknown','feed')),
    CONSTRAINT ck_of_offer_observation_terms CHECK (source_id > 0 AND price_cents > 0 AND currency = 'USD'),
    CONSTRAINT fk_of_offer_observation_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime(account_id) ON DELETE RESTRICT,
    CONSTRAINT fk_of_offer_observation_binding_id_of_creator_bindings FOREIGN KEY(binding_id) REFERENCES of_creator_bindings(id) ON DELETE RESTRICT
);
CREATE INDEX ix_of_offer_observation_media ON of_offer_observation(account_id,media_key);

CREATE TABLE of_offer_import_job (
    account_id VARCHAR NOT NULL,
    job_id VARCHAR(36) NOT NULL,
    binding_id VARCHAR NOT NULL,
    command_key VARCHAR(64) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    actor VARCHAR(128) NOT NULL,
    mode VARCHAR(12) NOT NULL,
    status VARCHAR(12) NOT NULL,
    revision INTEGER NOT NULL,
    connection_revision INTEGER,
    upper_message_id BIGINT,
    after_fan_id BIGINT,
    after_message_id BIGINT,
    rows_scanned INTEGER NOT NULL,
    pages_read INTEGER NOT NULL,
    page_budget INTEGER NOT NULL,
    targets_total INTEGER NOT NULL,
    targets_done INTEGER NOT NULL,
    lease_token VARCHAR(36),
    lease_until TIMESTAMP WITHOUT TIME ZONE,
    next_attempt_at TIMESTAMP WITHOUT TIME ZONE,
    last_error VARCHAR(64),
    requested_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    completed_at TIMESTAMP WITHOUT TIME ZONE,
    CONSTRAINT pk_of_offer_import_job PRIMARY KEY (account_id,job_id),
    CONSTRAINT uq_of_offer_import_job_command UNIQUE (account_id,command_key),
    CONSTRAINT ck_of_offer_import_job_mode CHECK (mode IN ('inventory','refresh')),
    CONSTRAINT ck_of_offer_import_job_status CHECK (status IN ('pending','running','complete','partial','failed','cancelled','superseded')),
    CONSTRAINT ck_of_offer_import_job_lease CHECK ((status = 'running') = (lease_token IS NOT NULL AND lease_until IS NOT NULL)),
    CONSTRAINT ck_of_offer_import_job_bounds CHECK (revision > 0 AND rows_scanned >= 0 AND pages_read >= 0 AND pages_read <= page_budget AND targets_done >= 0 AND targets_done <= targets_total AND targets_total <= 50),
    CONSTRAINT fk_of_offer_import_job_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime(account_id) ON DELETE RESTRICT,
    CONSTRAINT fk_of_offer_import_job_binding_id_of_creator_bindings FOREIGN KEY(binding_id) REFERENCES of_creator_bindings(id) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX uq_of_offer_import_job_active ON of_offer_import_job(account_id,mode) WHERE status IN ('pending','running');

CREATE TABLE of_offer_import_cursor (
    account_id VARCHAR NOT NULL,
    job_id VARCHAR(36) NOT NULL,
    fan_id BIGINT NOT NULL,
    targets_json TEXT NOT NULL,
    found_json TEXT NOT NULL,
    before_id BIGINT,
    pages_read INTEGER NOT NULL,
    attempts INTEGER NOT NULL,
    status VARCHAR(12) NOT NULL,
    outcome VARCHAR(32),
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    CONSTRAINT pk_of_offer_import_cursor PRIMARY KEY (account_id,job_id,fan_id),
    CONSTRAINT ck_of_offer_import_cursor_status CHECK (status IN ('pending','complete','unresolved')),
    CONSTRAINT ck_of_offer_import_cursor_bounds CHECK (fan_id > 0 AND pages_read >= 0 AND pages_read <= 200 AND attempts >= 0),
    CONSTRAINT fk_of_offer_import_cursor_account_id_of_offer_import_job FOREIGN KEY(account_id,job_id) REFERENCES of_offer_import_job(account_id,job_id) ON DELETE RESTRICT
);

CREATE TABLE of_offer_import_candidate (
    account_id VARCHAR NOT NULL,
    job_id VARCHAR(36) NOT NULL,
    media_key VARCHAR(64) NOT NULL,
    media_json TEXT NOT NULL,
    source_count INTEGER NOT NULL,
    variants_json TEXT NOT NULL,
    samples_json TEXT NOT NULL,
    revision INTEGER NOT NULL,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    CONSTRAINT pk_of_offer_import_candidate PRIMARY KEY (account_id,job_id,media_key),
    CONSTRAINT ck_of_offer_import_candidate_bounds CHECK (source_count > 0 AND revision > 0),
    CONSTRAINT fk_of_offer_import_candidate_account_id_of_offer_import_job FOREIGN KEY(account_id,job_id) REFERENCES of_offer_import_job(account_id,job_id) ON DELETE RESTRICT
);

CREATE TABLE of_offer_import_decision (
    account_id VARCHAR NOT NULL,
    command_key VARCHAR(64) NOT NULL,
    binding_id VARCHAR NOT NULL,
    action VARCHAR(12) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    actor VARCHAR(128) NOT NULL,
    observation_id VARCHAR(36) NOT NULL,
    fingerprint VARCHAR(64) NOT NULL,
    media_key VARCHAR(64) NOT NULL,
    reuse_confirmed BOOLEAN NOT NULL,
    description_approved BOOLEAN NOT NULL,
    item_id INTEGER NOT NULL,
    result_json TEXT NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    CONSTRAINT pk_of_offer_import_decision PRIMARY KEY (account_id,command_key),
    CONSTRAINT ck_of_offer_import_decision_action CHECK (action IN ('create','withdraw')),
    CONSTRAINT ck_of_offer_import_decision_approval CHECK (action <> 'create' OR (reuse_confirmed AND description_approved)),
    CONSTRAINT fk_of_offer_import_decision_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime(account_id) ON DELETE RESTRICT,
    CONSTRAINT fk_of_offer_import_decision_binding_id_of_creator_bindings FOREIGN KEY(binding_id) REFERENCES of_creator_bindings(id) ON DELETE RESTRICT,
    CONSTRAINT fk_of_offer_import_decision_item_id_catalog_items FOREIGN KEY(item_id) REFERENCES catalog_items(id) ON DELETE RESTRICT,
    CONSTRAINT fk_of_offer_import_decision_account_id_of_offer_observation FOREIGN KEY(account_id,observation_id) REFERENCES of_offer_observation(account_id,observation_id) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX uq_of_offer_import_decision_bundle ON of_offer_import_decision(account_id,binding_id,media_key) WHERE action = 'create';
CREATE UNIQUE INDEX uq_of_offer_import_decision_withdraw ON of_offer_import_decision(account_id,item_id) WHERE action = 'withdraw';
CREATE INDEX ix_of_offer_import_decision_item ON of_offer_import_decision(account_id,item_id);

CREATE FUNCTION of_offer_observation_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'offer observation evidence is append-only'; END;
$$;
CREATE TRIGGER of_offer_observation_immutable BEFORE UPDATE OR DELETE ON of_offer_observation
FOR EACH ROW EXECUTE FUNCTION of_offer_observation_immutable();

CREATE FUNCTION of_offer_decision_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'offer import decisions are append-only'; END;
$$;
CREATE TRIGGER of_offer_decision_immutable BEFORE UPDATE OR DELETE ON of_offer_import_decision
FOR EACH ROW EXECUTE FUNCTION of_offer_decision_immutable();

CREATE FUNCTION of_offer_import_job_frozen() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'offer import job input is immutable'; END IF;
 IF ROW(NEW.account_id,NEW.job_id,NEW.binding_id,NEW.command_key,NEW.request_hash,NEW.actor,NEW.mode,NEW.connection_revision,NEW.upper_message_id,NEW.page_budget,NEW.targets_total,NEW.requested_at)
 IS DISTINCT FROM ROW(OLD.account_id,OLD.job_id,OLD.binding_id,OLD.command_key,OLD.request_hash,OLD.actor,OLD.mode,OLD.connection_revision,OLD.upper_message_id,OLD.page_budget,OLD.targets_total,OLD.requested_at)
 THEN RAISE EXCEPTION 'offer import job input is immutable'; END IF;
 RETURN NEW;
END;
$$;
CREATE TRIGGER of_offer_import_job_frozen BEFORE UPDATE OR DELETE ON of_offer_import_job
FOR EACH ROW EXECUTE FUNCTION of_offer_import_job_frozen();

-- Section 15: continuous import and two-way content sync (same unpublished revision).
CREATE TABLE of_content_sync_account (
	account_id VARCHAR NOT NULL, 
	binding_id VARCHAR NOT NULL, 
	enabled BOOLEAN NOT NULL, 
	status VARCHAR(12) NOT NULL, 
	revision INTEGER NOT NULL, 
	actor VARCHAR(128) NOT NULL, 
	command_key VARCHAR(64) NOT NULL, 
	connection_revision INTEGER, 
	initial_job_id VARCHAR(36), 
	incremental_due_at TIMESTAMP WITHOUT TIME ZONE, 
	reconcile_due_at TIMESTAMP WITHOUT TIME ZONE, 
	last_incremental_at TIMESTAMP WITHOUT TIME ZONE, 
	last_reconcile_at TIMESTAMP WITHOUT TIME ZONE, 
	last_full_reconcile_at TIMESTAMP WITHOUT TIME ZONE, 
	covered_through_message_id BIGINT, 
	reconcile_after_item_id INTEGER, 
	reconcile_cycle_started_at TIMESTAMP WITHOUT TIME ZONE, 
	posts_checked_at TIMESTAMP WITHOUT TIME ZONE, 
	posts_error VARCHAR(64), 
	backlog INTEGER NOT NULL, 
	failures INTEGER NOT NULL, 
	blocked_reason VARCHAR(64), 
	lease_token VARCHAR(36), 
	lease_until TIMESTAMP WITHOUT TIME ZONE, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	CONSTRAINT pk_of_content_sync_account PRIMARY KEY (account_id), 
	CONSTRAINT ck_of_content_sync_account_status CHECK (status IN ('off','active','paused_auth','blocked')), 
	CONSTRAINT ck_of_content_sync_account_bounds CHECK (revision > 0 AND backlog >= 0 AND failures >= 0), 
	CONSTRAINT ck_of_content_sync_account_lease CHECK ((lease_token IS NULL) = (lease_until IS NULL)), 
	CONSTRAINT fk_of_content_sync_account_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime (account_id) ON DELETE RESTRICT, 
	CONSTRAINT fk_of_content_sync_account_binding_id_of_creator_bindings FOREIGN KEY(binding_id) REFERENCES of_creator_bindings (id) ON DELETE RESTRICT
);
CREATE INDEX ix_of_content_sync_account_due ON of_content_sync_account (status, incremental_due_at);

CREATE TABLE of_content_sync_operation (
	account_id VARCHAR NOT NULL, 
	operation_id VARCHAR(36) NOT NULL, 
	binding_id VARCHAR NOT NULL, 
	command_key VARCHAR(64) NOT NULL, 
	request_hash VARCHAR(64) NOT NULL, 
	actor VARCHAR(128) NOT NULL, 
	kind VARCHAR(12) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	connection_revision INTEGER, 
	draft_id VARCHAR(36), 
	asset_id VARCHAR(36), 
	coverage_json TEXT NOT NULL, 
	result_json TEXT NOT NULL, 
	attempts INTEGER NOT NULL, 
	last_error VARCHAR(64), 
	lease_token VARCHAR(36), 
	lease_until TIMESTAMP WITHOUT TIME ZONE, 
	next_attempt_at TIMESTAMP WITHOUT TIME ZONE, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	completed_at TIMESTAMP WITHOUT TIME ZONE, 
	CONSTRAINT pk_of_content_sync_operation PRIMARY KEY (account_id, operation_id), 
	CONSTRAINT uq_of_content_sync_operation_command UNIQUE (account_id, command_key), 
	CONSTRAINT ck_of_content_sync_operation_kind CHECK (kind IN ('initial','incremental','reconcile','sync_now','upload','retry')), 
	CONSTRAINT ck_of_content_sync_operation_status CHECK (status IN ('queued','running','complete','partial','failed','cancelled','outcome_unconfirmed')), 
	CONSTRAINT ck_of_content_sync_operation_lease CHECK ((status = 'running') = (lease_token IS NOT NULL AND lease_until IS NOT NULL)), 
	CONSTRAINT fk_of_content_sync_operation_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime (account_id) ON DELETE RESTRICT, 
	CONSTRAINT fk_of_content_sync_operation_binding_id_of_creator_bindings FOREIGN KEY(binding_id) REFERENCES of_creator_bindings (id) ON DELETE RESTRICT
);
CREATE INDEX ix_of_content_sync_operation_recent ON of_content_sync_operation (account_id, created_at);
CREATE UNIQUE INDEX uq_of_content_sync_operation_active ON of_content_sync_operation (account_id, kind) WHERE status IN ('queued','running') AND kind IN ('incremental','reconcile','sync_now');

CREATE TABLE of_content_sync_asset (
	account_id VARCHAR NOT NULL, 
	asset_id VARCHAR(36) NOT NULL, 
	binding_id VARCHAR NOT NULL, 
	kind VARCHAR(8) NOT NULL, 
	name VARCHAR(240) NOT NULL, 
	size_bytes BIGINT NOT NULL, 
	content_hash VARCHAR(64) NOT NULL, 
	staged_ref VARCHAR(160), 
	upload_token VARCHAR(36) NOT NULL, 
	provider_media_id BIGINT, 
	state VARCHAR(20) NOT NULL, 
	attempts INTEGER NOT NULL, 
	last_error VARCHAR(64), 
	next_attempt_at TIMESTAMP WITHOUT TIME ZONE, 
	lease_token VARCHAR(36), 
	lease_until TIMESTAMP WITHOUT TIME ZONE, 
	upload_started_at TIMESTAMP WITHOUT TIME ZONE, 
	verified_at TIMESTAMP WITHOUT TIME ZONE, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	CONSTRAINT pk_of_content_sync_asset PRIMARY KEY (account_id, asset_id), 
	CONSTRAINT uq_of_content_sync_asset_hash UNIQUE (account_id, binding_id, content_hash), 
	CONSTRAINT uq_of_content_sync_asset_token UNIQUE (account_id, upload_token), 
	CONSTRAINT ck_of_content_sync_asset_kind CHECK (kind IN ('photo','video')), 
	CONSTRAINT ck_of_content_sync_asset_state CHECK (state IN ('queued','uploading','verifying','synced','retrying','connection_required','needs_review','outcome_unconfirmed')), 
	CONSTRAINT ck_of_content_sync_asset_bounds CHECK (size_bytes > 0 AND attempts >= 0), 
	CONSTRAINT ck_of_content_sync_asset_provider CHECK (provider_media_id IS NULL OR provider_media_id > 0), 
	CONSTRAINT ck_of_content_sync_asset_synced CHECK (state <> 'synced' OR (provider_media_id IS NOT NULL AND verified_at IS NOT NULL)), 
	CONSTRAINT ck_of_content_sync_asset_lease CHECK ((lease_token IS NULL) = (lease_until IS NULL)), 
	CONSTRAINT fk_of_content_sync_asset_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime (account_id) ON DELETE RESTRICT, 
	CONSTRAINT fk_of_content_sync_asset_binding_id_of_creator_bindings FOREIGN KEY(binding_id) REFERENCES of_creator_bindings (id) ON DELETE RESTRICT
);
CREATE INDEX ix_of_content_sync_asset_due ON of_content_sync_asset (state, next_attempt_at);
CREATE UNIQUE INDEX uq_of_content_sync_asset_provider ON of_content_sync_asset (account_id, binding_id, provider_media_id) WHERE provider_media_id IS NOT NULL;

CREATE TABLE of_content_sync_draft (
	account_id VARCHAR NOT NULL, 
	draft_id VARCHAR(36) NOT NULL, 
	binding_id VARCHAR NOT NULL, 
	command_key VARCHAR(64) NOT NULL, 
	request_hash VARCHAR(64) NOT NULL, 
	actor VARCHAR(128) NOT NULL, 
	revision INTEGER NOT NULL, 
	label TEXT NOT NULL, 
	description TEXT NOT NULL, 
	price_cents INTEGER NOT NULL, 
	currency VARCHAR(3) NOT NULL, 
	assets_json TEXT NOT NULL, 
	preview_json TEXT NOT NULL, 
	state VARCHAR(20) NOT NULL, 
	reason VARCHAR(64), 
	item_id INTEGER, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	CONSTRAINT pk_of_content_sync_draft PRIMARY KEY (account_id, draft_id), 
	CONSTRAINT uq_of_content_sync_draft_command UNIQUE (account_id, command_key), 
	CONSTRAINT ck_of_content_sync_draft_state CHECK (state IN ('draft','queued','uploading','verifying','synced','retrying','connection_required','needs_review','outcome_unconfirmed')), 
	CONSTRAINT ck_of_content_sync_draft_terms CHECK (revision > 0 AND price_cents BETWEEN 300 AND 20000 AND currency = 'USD'), 
	CONSTRAINT fk_of_content_sync_draft_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime (account_id) ON DELETE RESTRICT, 
	CONSTRAINT fk_of_content_sync_draft_binding_id_of_creator_bindings FOREIGN KEY(binding_id) REFERENCES of_creator_bindings (id) ON DELETE RESTRICT, 
	CONSTRAINT fk_of_content_sync_draft_item_id_catalog_items FOREIGN KEY(item_id) REFERENCES catalog_items (id) ON DELETE RESTRICT
);
CREATE INDEX ix_of_content_sync_draft_item ON of_content_sync_draft (account_id, item_id);

CREATE TABLE of_content_sync_conflict (
	account_id VARCHAR NOT NULL, 
	conflict_id VARCHAR(36) NOT NULL, 
	binding_id VARCHAR NOT NULL, 
	item_id INTEGER NOT NULL, 
	kind VARCHAR(16) NOT NULL, 
	prior_fingerprint VARCHAR(64) NOT NULL, 
	observation_id VARCHAR(36) NOT NULL, 
	status VARCHAR(12) NOT NULL, 
	revision INTEGER NOT NULL, 
	resolution VARCHAR(16), 
	resolved_by VARCHAR(128), 
	resolved_command_key VARCHAR(64), 
	resolved_at TIMESTAMP WITHOUT TIME ZONE, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	CONSTRAINT pk_of_content_sync_conflict PRIMARY KEY (account_id, conflict_id), 
	CONSTRAINT uq_of_content_sync_conflict_source UNIQUE (account_id, item_id, observation_id), 
	CONSTRAINT fk_of_content_sync_conflict_account_id_of_offer_observation FOREIGN KEY(account_id, observation_id) REFERENCES of_offer_observation (account_id, observation_id) ON DELETE RESTRICT, 
	CONSTRAINT ck_of_content_sync_conflict_status CHECK (status IN ('open','resolved')), 
	CONSTRAINT ck_of_content_sync_conflict_resolution CHECK (resolution IS NULL OR resolution IN ('accept_source','keep_crm','disable')), 
	CONSTRAINT ck_of_content_sync_conflict_resolved CHECK ((status = 'resolved') = (resolution IS NOT NULL AND resolved_at IS NOT NULL)), 
	CONSTRAINT ck_of_content_sync_conflict_bounds CHECK (revision > 0), 
	CONSTRAINT fk_of_content_sync_conflict_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime (account_id) ON DELETE RESTRICT, 
	CONSTRAINT fk_of_content_sync_conflict_binding_id_of_creator_bindings FOREIGN KEY(binding_id) REFERENCES of_creator_bindings (id) ON DELETE RESTRICT, 
	CONSTRAINT fk_of_content_sync_conflict_item_id_catalog_items FOREIGN KEY(item_id) REFERENCES catalog_items (id) ON DELETE RESTRICT
);
CREATE INDEX ix_of_content_sync_conflict_open ON of_content_sync_conflict (account_id, item_id) WHERE status = 'open';

CREATE TABLE of_content_sync_decision (
	account_id VARCHAR NOT NULL, 
	command_key VARCHAR(64) NOT NULL, 
	binding_id VARCHAR NOT NULL, 
	action VARCHAR(12) NOT NULL, 
	request_hash VARCHAR(64) NOT NULL, 
	actor VARCHAR(128) NOT NULL, 
	subject_id VARCHAR(64), 
	before_json TEXT NOT NULL, 
	after_json TEXT NOT NULL, 
	result_json TEXT NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	CONSTRAINT pk_of_content_sync_decision PRIMARY KEY (account_id, command_key), 
	CONSTRAINT ck_of_content_sync_decision_action CHECK (action IN ('settings','resolve','save','retry')), 
	CONSTRAINT fk_of_content_sync_decision_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime (account_id) ON DELETE RESTRICT, 
	CONSTRAINT fk_of_content_sync_decision_binding_id_of_creator_bindings FOREIGN KEY(binding_id) REFERENCES of_creator_bindings (id) ON DELETE RESTRICT
);
CREATE INDEX ix_of_content_sync_decision_recent ON of_content_sync_decision (account_id, created_at);

CREATE FUNCTION of_content_sync_decision_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'content sync decisions are append-only'; END;
$$;
CREATE TRIGGER of_content_sync_decision_immutable BEFORE UPDATE OR DELETE ON of_content_sync_decision
FOR EACH ROW EXECUTE FUNCTION of_content_sync_decision_immutable();

-- A confirmed provider mapping never changes owner or id; rebinding needs a new row.
CREATE FUNCTION of_content_sync_asset_frozen() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'content sync asset identity is immutable'; END IF;
 IF ROW(NEW.account_id,NEW.asset_id,NEW.binding_id,NEW.content_hash,NEW.upload_token,NEW.kind,NEW.size_bytes)
 IS DISTINCT FROM ROW(OLD.account_id,OLD.asset_id,OLD.binding_id,OLD.content_hash,OLD.upload_token,OLD.kind,OLD.size_bytes)
 OR (OLD.provider_media_id IS NOT NULL AND NEW.provider_media_id IS DISTINCT FROM OLD.provider_media_id)
 THEN RAISE EXCEPTION 'content sync asset identity is immutable'; END IF;
 RETURN NEW;
END;
$$;
CREATE TRIGGER of_content_sync_asset_frozen BEFORE UPDATE OR DELETE ON of_content_sync_asset
FOR EACH ROW EXECUTE FUNCTION of_content_sync_asset_frozen();

-- A resolved conflict is history: its decision cannot be rewritten or removed.
CREATE FUNCTION of_content_sync_conflict_frozen() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP = 'DELETE' OR OLD.status = 'resolved' THEN RAISE EXCEPTION 'resolved sync conflicts are immutable'; END IF;
 IF ROW(NEW.account_id,NEW.conflict_id,NEW.binding_id,NEW.item_id,NEW.kind,NEW.prior_fingerprint,NEW.observation_id,NEW.created_at)
 IS DISTINCT FROM ROW(OLD.account_id,OLD.conflict_id,OLD.binding_id,OLD.item_id,OLD.kind,OLD.prior_fingerprint,OLD.observation_id,OLD.created_at)
 THEN RAISE EXCEPTION 'resolved sync conflicts are immutable'; END IF;
 RETURN NEW;
END;
$$;
CREATE TRIGGER of_content_sync_conflict_frozen BEFORE UPDATE OR DELETE ON of_content_sync_conflict
FOR EACH ROW EXECUTE FUNCTION of_content_sync_conflict_frozen();
