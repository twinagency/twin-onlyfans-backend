-- Frozen additive final integration schema. Existing history and evidence retained.

CREATE TABLE of_content_post_progress (
	account_id VARCHAR NOT NULL, 
	binding_id VARCHAR NOT NULL, 
	connection_revision INTEGER NOT NULL, 
	history_marker VARCHAR(256), 
	history_complete BOOLEAN NOT NULL, 
	history_checked_at TIMESTAMP WITHOUT TIME ZONE, 
	revisit_after_id BIGINT, 
	lease_token VARCHAR(36), 
	lease_until TIMESTAMP WITHOUT TIME ZONE, 
	next_attempt_at TIMESTAMP WITHOUT TIME ZONE, 
	last_error VARCHAR(64), 
	cursor_contract VARCHAR(64), 
	cursor_evidence_json TEXT, 
	CONSTRAINT pk_of_content_post_progress PRIMARY KEY (account_id), 
	CONSTRAINT ck_of_content_post_progress_lease CHECK ((lease_token IS NULL) = (lease_until IS NULL)), 
	CONSTRAINT fk_of_content_post_progress_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime (account_id) ON DELETE RESTRICT, 
	CONSTRAINT fk_of_content_post_progress_binding_id_of_creator_bindings FOREIGN KEY(binding_id) REFERENCES of_creator_bindings (id) ON DELETE RESTRICT
);

CREATE TABLE of_content_sync_occurrence (
	account_id VARCHAR NOT NULL, 
	binding_id VARCHAR NOT NULL, 
	source_kind VARCHAR(8) NOT NULL, 
	source_id BIGINT NOT NULL, 
	occurrence INTEGER NOT NULL, 
	observation_id VARCHAR(36) NOT NULL, 
	observed_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	CONSTRAINT pk_of_content_sync_occurrence PRIMARY KEY (account_id, binding_id, source_kind, source_id, occurrence), 
	CONSTRAINT ck_of_content_sync_occurrence_bounds CHECK (source_kind IN ('message','post') AND source_id > 0 AND occurrence > 0), 
	CONSTRAINT fk_of_content_sync_occurrence_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime (account_id) ON DELETE RESTRICT, 
	CONSTRAINT fk_of_content_sync_occurrence_binding_id_of_creator_bindings FOREIGN KEY(binding_id) REFERENCES of_creator_bindings (id) ON DELETE RESTRICT
);

CREATE TABLE of_content_sync_source (
	account_id VARCHAR NOT NULL, 
	binding_id VARCHAR NOT NULL, 
	source_kind VARCHAR(8) NOT NULL, 
	source_id BIGINT NOT NULL, 
	fan_id BIGINT, 
	current_observation_id VARCHAR(36), 
	current_fingerprint VARCHAR(64), 
	occurrence INTEGER NOT NULL, 
	observed_at TIMESTAMP WITHOUT TIME ZONE, 
	folded_message_media_json TEXT, 
	folded_message_price_cents INTEGER, 
	folded_message_unsent BOOLEAN, 
	folded_at TIMESTAMP WITHOUT TIME ZONE, 
	candidate_job_id VARCHAR(36), 
	candidate_media_key VARCHAR(64), 
	refresh_requested_at TIMESTAMP WITHOUT TIME ZONE, 
	refresh_completed_at TIMESTAMP WITHOUT TIME ZONE, 
	refresh_job_id VARCHAR(36), 
	refresh_error VARCHAR(64), 
	last_checked_at TIMESTAMP WITHOUT TIME ZONE, 
	source_unavailable BOOLEAN NOT NULL, 
	unavailable_reason VARCHAR(64), 
	CONSTRAINT pk_of_content_sync_source PRIMARY KEY (account_id, binding_id, source_kind, source_id), 
	CONSTRAINT ck_of_content_sync_source_kind CHECK (source_kind IN ('message','post')), 
	CONSTRAINT ck_of_content_sync_source_bounds CHECK (source_id > 0 AND occurrence >= 0), 
	CONSTRAINT fk_of_content_sync_source_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime (account_id) ON DELETE RESTRICT, 
	CONSTRAINT fk_of_content_sync_source_binding_id_of_creator_bindings FOREIGN KEY(binding_id) REFERENCES of_creator_bindings (id) ON DELETE RESTRICT
);

CREATE INDEX ix_of_content_sync_source_refresh ON of_content_sync_source (account_id, binding_id, refresh_requested_at);

CREATE TABLE of_launch_command (
	account_id VARCHAR NOT NULL, 
	command_key VARCHAR(64) NOT NULL, 
	binding_id VARCHAR NOT NULL, 
	kind VARCHAR(32) NOT NULL, 
	request_hash VARCHAR(64) NOT NULL, 
	actor VARCHAR(128) NOT NULL, 
	reason VARCHAR(512) NOT NULL, 
	before_json TEXT NOT NULL, 
	result_json TEXT NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	CONSTRAINT pk_of_launch_command PRIMARY KEY (account_id, command_key), 
	CONSTRAINT fk_of_launch_command_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime (account_id) ON DELETE RESTRICT, 
	CONSTRAINT fk_of_launch_command_binding_id_of_creator_bindings FOREIGN KEY(binding_id) REFERENCES of_creator_bindings (id) ON DELETE RESTRICT
);

CREATE INDEX ix_of_launch_command_kind ON of_launch_command (account_id, kind, created_at);

CREATE TABLE of_launch_worker_health (
	account_id VARCHAR NOT NULL, 
	binding_id VARCHAR NOT NULL, 
	observed_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	capabilities_json TEXT NOT NULL, 
	last_error VARCHAR(64), 
	CONSTRAINT pk_of_launch_worker_health PRIMARY KEY (account_id), 
	CONSTRAINT fk_of_launch_worker_health_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime (account_id) ON DELETE RESTRICT, 
	CONSTRAINT fk_of_launch_worker_health_binding_id_of_creator_bindings FOREIGN KEY(binding_id) REFERENCES of_creator_bindings (id) ON DELETE RESTRICT
);

CREATE TABLE of_provider_read_budget (
	account_id VARCHAR NOT NULL, 
	binding_id VARCHAR NOT NULL, 
	connection_revision INTEGER NOT NULL, 
	lease_token VARCHAR(36), 
	lease_until TIMESTAMP WITHOUT TIME ZONE, 
	tokens INTEGER NOT NULL, 
	refilled_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	backoff_until TIMESTAMP WITHOUT TIME ZONE, 
	last_kind VARCHAR(32), 
	CONSTRAINT pk_of_provider_read_budget PRIMARY KEY (account_id), 
	CONSTRAINT ck_of_provider_read_budget_tokens CHECK (tokens >= 0 AND tokens <= 10), 
	CONSTRAINT ck_of_provider_read_budget_lease CHECK ((lease_token IS NULL) = (lease_until IS NULL)), 
	CONSTRAINT fk_of_provider_read_budget_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime (account_id) ON DELETE RESTRICT, 
	CONSTRAINT fk_of_provider_read_budget_binding_id_of_creator_bindings FOREIGN KEY(binding_id) REFERENCES of_creator_bindings (id) ON DELETE RESTRICT
);

CREATE TABLE of_ledger_resolution (
	account_id VARCHAR NOT NULL, 
	evidence_hash VARCHAR(64) NOT NULL, 
	binding_id VARCHAR NOT NULL, 
	command_key VARCHAR(64) NOT NULL, 
	transaction_id INTEGER NOT NULL, 
	fan_id BIGINT NOT NULL, 
	message_id BIGINT NOT NULL, 
	decision_json TEXT NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	CONSTRAINT pk_of_ledger_resolution PRIMARY KEY (account_id, evidence_hash), 
	CONSTRAINT fk_of_ledger_resolution_account_id_of_ledger_evidence FOREIGN KEY(account_id, evidence_hash) REFERENCES of_ledger_evidence (account_id, evidence_hash) ON DELETE RESTRICT, 
	CONSTRAINT fk_of_ledger_resolution_account_id_of_launch_command FOREIGN KEY(account_id, command_key) REFERENCES of_launch_command (account_id, command_key) ON DELETE RESTRICT, 
	CONSTRAINT fk_of_ledger_resolution_binding_id_of_creator_bindings FOREIGN KEY(binding_id) REFERENCES of_creator_bindings (id) ON DELETE RESTRICT, 
	CONSTRAINT fk_of_ledger_resolution_transaction_id_transactions FOREIGN KEY(transaction_id) REFERENCES transactions (id) ON DELETE RESTRICT
);

ALTER TABLE of_content_sync_conflict ADD COLUMN source_occurrence INTEGER NOT NULL DEFAULT 0;
ALTER TABLE of_content_sync_conflict DROP CONSTRAINT uq_of_content_sync_conflict_source;
ALTER TABLE of_content_sync_conflict ADD CONSTRAINT uq_of_content_sync_conflict_source UNIQUE(account_id,item_id,observation_id,source_occurrence);

CREATE OR REPLACE FUNCTION of_content_sync_conflict_frozen() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP = 'DELETE' OR OLD.status = 'resolved' THEN RAISE EXCEPTION 'resolved sync conflicts are immutable'; END IF;
 IF ROW(NEW.account_id,NEW.conflict_id,NEW.binding_id,NEW.item_id,NEW.kind,NEW.prior_fingerprint,NEW.observation_id,NEW.created_at,NEW.source_occurrence)
 IS DISTINCT FROM ROW(OLD.account_id,OLD.conflict_id,OLD.binding_id,OLD.item_id,OLD.kind,OLD.prior_fingerprint,OLD.observation_id,OLD.created_at,OLD.source_occurrence)
 THEN RAISE EXCEPTION 'resolved sync conflicts are immutable'; END IF;
 RETURN NEW;
END;
$$;


CREATE FUNCTION of_launch_command_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'launch command evidence is append-only'; END; $$;
CREATE TRIGGER of_launch_command_immutable BEFORE UPDATE OR DELETE ON of_launch_command FOR EACH ROW EXECUTE FUNCTION of_launch_command_immutable();

CREATE FUNCTION of_ledger_resolution_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'ledger resolution evidence is append-only'; END; $$;
CREATE TRIGGER of_ledger_resolution_immutable BEFORE UPDATE OR DELETE ON of_ledger_resolution FOR EACH ROW EXECUTE FUNCTION of_ledger_resolution_immutable();

CREATE FUNCTION of_content_sync_occurrence_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'content source occurrence evidence is append-only'; END; $$;
CREATE TRIGGER of_content_sync_occurrence_immutable BEFORE UPDATE OR DELETE ON of_content_sync_occurrence FOR EACH ROW EXECUTE FUNCTION of_content_sync_occurrence_immutable();

-- Concurrent ledger retry contribution is introduced here, after published
-- 0090. Its original command evidence and authority remain append-only.
CREATE TABLE of_ledger_retry_command (
 account_id VARCHAR NOT NULL,
 command_key VARCHAR(64) NOT NULL,
 binding_id VARCHAR NOT NULL,
 request_hash VARCHAR(64) NOT NULL,
 actor VARCHAR(128) NOT NULL,
 expected_connection_revision INTEGER NOT NULL,
 expected_scan_fingerprint VARCHAR(64) NOT NULL,
 result VARCHAR(16) NOT NULL,
 created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
 CONSTRAINT pk_of_ledger_retry_command PRIMARY KEY (account_id, command_key),
 CONSTRAINT ck_of_ledger_retry_command_result CHECK (result IN ('queued')),
 CONSTRAINT ck_of_ledger_retry_command_revision CHECK (expected_connection_revision >= 0),
 CONSTRAINT fk_of_ledger_retry_command_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime (account_id) ON DELETE RESTRICT,
 CONSTRAINT fk_of_ledger_retry_command_binding_id_of_creator_bindings FOREIGN KEY(binding_id) REFERENCES of_creator_bindings (id) ON DELETE RESTRICT
);
CREATE FUNCTION of_ledger_retry_command_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'ledger retry command evidence is append-only'; END; $$;
CREATE TRIGGER of_ledger_retry_command_immutable BEFORE UPDATE OR DELETE ON of_ledger_retry_command FOR EACH ROW EXECUTE FUNCTION of_ledger_retry_command_immutable();

CREATE INDEX ix_messages_account_ingested ON messages (account_id, ingested_at);
ALTER TABLE of_content_sync_account ADD COLUMN reconcile_priority_after_item_id INTEGER;
