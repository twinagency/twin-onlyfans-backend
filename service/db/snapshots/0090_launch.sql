-- Launch tables: history recovery and fan memory, personality workflow, proactive engagement.
-- Additive only. Nothing is enabled: engagement and memory stay OFF until configured and the worker flags are set.

-- Engagement is a new text-intent origin (same pattern as 0080 campaigns).
ALTER TABLE of_text_intent DROP CONSTRAINT ck_of_text_intent_origin;
ALTER TABLE of_text_intent ADD CONSTRAINT ck_of_text_intent_origin CHECK (origin IN ('automated','manual','campaign','engagement'));

CREATE TABLE of_history_memory_correction (
	account_id VARCHAR NOT NULL, 
	command_key VARCHAR(64) NOT NULL, 
	fan_id BIGINT NOT NULL, 
	field VARCHAR(32) NOT NULL, 
	operation VARCHAR(8) NOT NULL, 
	value TEXT, 
	before_json TEXT NOT NULL, 
	request_hash VARCHAR(64) NOT NULL, 
	revision BIGINT NOT NULL, 
	actor VARCHAR(128) NOT NULL, 
	reason VARCHAR(512) NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	CONSTRAINT pk_of_history_memory_correction PRIMARY KEY (account_id, command_key), 
	CONSTRAINT fk_of_history_memory_correction_account_id_fans FOREIGN KEY(account_id, fan_id) REFERENCES fans (account_id, fan_id) ON DELETE RESTRICT, 
	CONSTRAINT ck_of_history_memory_correction_operation CHECK (operation IN ('set','clear'))
);

CREATE INDEX ix_history_memory_correction ON of_history_memory_correction (account_id, fan_id, field, revision);

CREATE TABLE of_history_memory_state (
	account_id VARCHAR NOT NULL, 
	fan_id BIGINT NOT NULL, 
	revision BIGINT NOT NULL, 
	last_processed_at TIMESTAMP WITHOUT TIME ZONE, 
	CONSTRAINT pk_of_history_memory_state PRIMARY KEY (account_id, fan_id), 
	CONSTRAINT fk_of_history_memory_state_account_id_fans FOREIGN KEY(account_id, fan_id) REFERENCES fans (account_id, fan_id) ON DELETE RESTRICT
);

CREATE TABLE of_engagement_settings (
	account_id VARCHAR NOT NULL, 
	binding_id VARCHAR NOT NULL, 
	revision INTEGER NOT NULL, 
	settings_json TEXT NOT NULL, 
	CONSTRAINT pk_of_engagement_settings PRIMARY KEY (account_id), 
	CONSTRAINT fk_of_engagement_settings_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime (account_id) ON DELETE RESTRICT, 
	CONSTRAINT fk_of_engagement_settings_binding_id_of_creator_bindings FOREIGN KEY(binding_id) REFERENCES of_creator_bindings (id) ON DELETE RESTRICT
);

CREATE TABLE of_engagement_settings_change (
	account_id VARCHAR NOT NULL, 
	revision INTEGER NOT NULL, 
	binding_id VARCHAR NOT NULL, 
	command_key VARCHAR(64) NOT NULL, 
	request_hash VARCHAR(64) NOT NULL, 
	settings_json TEXT NOT NULL, 
	actor VARCHAR(128) NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	CONSTRAINT pk_of_engagement_settings_change PRIMARY KEY (account_id, revision), 
	CONSTRAINT uq_of_engagement_change_command UNIQUE (account_id, command_key), 
	CONSTRAINT fk_of_engagement_settings_change_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime (account_id) ON DELETE RESTRICT, 
	CONSTRAINT fk_of_engagement_settings_change_binding_id_of_creator_bindings FOREIGN KEY(binding_id) REFERENCES of_creator_bindings (id) ON DELETE RESTRICT
);

CREATE TABLE of_history_memory_evidence (
	account_id VARCHAR NOT NULL, 
	fan_id BIGINT NOT NULL, 
	message_id BIGINT NOT NULL, 
	field VARCHAR(32) NOT NULL, 
	algorithm VARCHAR(40) NOT NULL, 
	source_digest VARCHAR(64) NOT NULL, 
	value VARCHAR(80) NOT NULL, 
	quote VARCHAR(500) NOT NULL, 
	source_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	CONSTRAINT pk_of_history_memory_evidence PRIMARY KEY (account_id, fan_id, message_id, field, algorithm, source_digest), 
	CONSTRAINT fk_of_history_memory_evidence_account_id_messages FOREIGN KEY(account_id, fan_id, message_id) REFERENCES messages (account_id, fan_id, message_id) ON DELETE RESTRICT
);

CREATE INDEX ix_history_memory_field ON of_history_memory_evidence (account_id, fan_id, field, source_at);

CREATE TABLE of_history_memory_receipt (
	account_id VARCHAR NOT NULL, 
	fan_id BIGINT NOT NULL, 
	message_id BIGINT NOT NULL, 
	algorithm VARCHAR(40) NOT NULL, 
	source_digest VARCHAR(64) NOT NULL, 
	body_md5 VARCHAR(32) NOT NULL, 
	source_direction VARCHAR(16) NOT NULL, 
	source_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	source_unsent BOOLEAN NOT NULL, 
	processed_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	CONSTRAINT pk_of_history_memory_receipt PRIMARY KEY (account_id, fan_id, message_id), 
	CONSTRAINT fk_of_history_memory_receipt_account_id_messages FOREIGN KEY(account_id, fan_id, message_id) REFERENCES messages (account_id, fan_id, message_id) ON DELETE RESTRICT
);

CREATE TABLE of_launch_history_cursor (
	account_id VARCHAR NOT NULL, 
	fan_id BIGINT NOT NULL, 
	binding_id VARCHAR NOT NULL, 
	status VARCHAR(16) DEFAULT 'needed' NOT NULL, 
	generation INTEGER DEFAULT 0 NOT NULL, 
	start_before_id BIGINT, 
	upper_bound BIGINT, 
	before_id BIGINT, 
	pages_completed INTEGER DEFAULT 0 NOT NULL, 
	messages_observed BIGINT DEFAULT 0 NOT NULL, 
	messages_added BIGINT DEFAULT 0 NOT NULL, 
	oldest_message_at TIMESTAMP WITHOUT TIME ZONE, 
	lease_token VARCHAR(36), 
	lease_until TIMESTAMP WITHOUT TIME ZONE, 
	retry_at TIMESTAMP WITHOUT TIME ZONE, 
	last_error VARCHAR(40), 
	completed_at TIMESTAMP WITHOUT TIME ZONE, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	CONSTRAINT pk_of_launch_history_cursor PRIMARY KEY (account_id, fan_id), 
	CONSTRAINT ck_of_launch_history_cursor_status CHECK (status IN ('needed','running','complete')), 
	CONSTRAINT ck_of_launch_history_cursor_counts CHECK (pages_completed >= 0 AND messages_observed >= 0 AND messages_added >= 0), 
	CONSTRAINT fk_of_launch_history_cursor_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime (account_id) ON DELETE RESTRICT, 
	CONSTRAINT fk_of_launch_history_cursor_binding_id_of_creator_bindings FOREIGN KEY(binding_id) REFERENCES of_creator_bindings (id) ON DELETE RESTRICT
);

CREATE INDEX ix_launch_history_cursor_due ON of_launch_history_cursor (account_id, status, retry_at, generation, fan_id);

CREATE TABLE of_launch_history_scan (
	account_id VARCHAR NOT NULL, 
	binding_id VARCHAR NOT NULL, 
	status VARCHAR(16) DEFAULT 'needed' NOT NULL, 
	generation INTEGER DEFAULT 0 NOT NULL, 
	cursor VARCHAR(1024), 
	page_count INTEGER DEFAULT 0 NOT NULL, 
	known_fan_after BIGINT DEFAULT 0 NOT NULL, 
	known_fans_complete BOOLEAN DEFAULT false NOT NULL, 
	lease_token VARCHAR(36), 
	lease_until TIMESTAMP WITHOUT TIME ZONE, 
	retry_at TIMESTAMP WITHOUT TIME ZONE, 
	last_error VARCHAR(40), 
	completed_at TIMESTAMP WITHOUT TIME ZONE, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	CONSTRAINT pk_of_launch_history_scan PRIMARY KEY (account_id), 
	CONSTRAINT ck_of_launch_history_scan_status CHECK (status IN ('needed','running','complete')), 
	CONSTRAINT fk_of_launch_history_scan_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime (account_id) ON DELETE RESTRICT, 
	CONSTRAINT fk_of_launch_history_scan_binding_id_of_creator_bindings FOREIGN KEY(binding_id) REFERENCES of_creator_bindings (id) ON DELETE RESTRICT
);

CREATE TABLE of_personality_head (
	account_id VARCHAR NOT NULL, 
	binding_id VARCHAR NOT NULL, 
	revision INTEGER NOT NULL, 
	CONSTRAINT pk_of_personality_head PRIMARY KEY (account_id), 
	CONSTRAINT ck_of_personality_head_revision CHECK (revision > 0), 
	CONSTRAINT fk_of_personality_head_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime (account_id) ON DELETE RESTRICT, 
	CONSTRAINT fk_of_personality_head_binding_id_of_creator_bindings FOREIGN KEY(binding_id) REFERENCES of_creator_bindings (id) ON DELETE RESTRICT
);

CREATE TABLE of_history_recent_bootstrap (
	account_id VARCHAR NOT NULL, 
	fan_id BIGINT NOT NULL, 
	binding_id VARCHAR NOT NULL, 
	upper_id BIGINT NOT NULL, 
	before_id BIGINT, 
	observed_count INTEGER NOT NULL, 
	CONSTRAINT pk_of_history_recent_bootstrap PRIMARY KEY (account_id, fan_id), 
	CONSTRAINT fk_of_history_recent_bootstrap_account_id_of_recovery FOREIGN KEY(account_id, fan_id) REFERENCES of_recovery (account_id, fan_id) ON DELETE RESTRICT, 
	CONSTRAINT ck_of_history_recent_bootstrap_count CHECK (observed_count >= 0 AND observed_count < 200), 
	CONSTRAINT fk_of_history_recent_bootstrap_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime (account_id) ON DELETE RESTRICT, 
	CONSTRAINT fk_of_history_recent_bootstrap_binding_id_of_creator_bindings FOREIGN KEY(binding_id) REFERENCES of_creator_bindings (id) ON DELETE RESTRICT
);

CREATE TABLE of_personality_revision (
	account_id VARCHAR NOT NULL, 
	revision INTEGER NOT NULL, 
	previous_revision INTEGER NOT NULL, 
	binding_id VARCHAR NOT NULL, 
	state VARCHAR(16) NOT NULL, 
	command_key VARCHAR(64) NOT NULL, 
	request_hash VARCHAR(64) NOT NULL, 
	actor VARCHAR(128) NOT NULL, 
	document_json TEXT NOT NULL, 
	configuration_revision INTEGER NOT NULL, 
	configuration_fingerprint VARCHAR(64) NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	CONSTRAINT pk_of_personality_revision PRIMARY KEY (account_id, revision), 
	CONSTRAINT ck_of_personality_revision_sequence CHECK (revision > 0 AND previous_revision >= 0 AND revision = previous_revision + 1), 
	CONSTRAINT ck_of_personality_revision_state CHECK (state IN ('draft','approved')), 
	CONSTRAINT uq_of_personality_command UNIQUE (account_id, command_key), 
	CONSTRAINT fk_of_personality_revision_account_id_of_personality_head FOREIGN KEY(account_id) REFERENCES of_personality_head (account_id) ON DELETE RESTRICT, 
	CONSTRAINT fk_of_personality_revision_binding_id_of_creator_bindings FOREIGN KEY(binding_id) REFERENCES of_creator_bindings (id) ON DELETE RESTRICT
);

CREATE TABLE of_engagement_action (
	account_id VARCHAR NOT NULL, 
	action_id VARCHAR(36) NOT NULL, 
	binding_id VARCHAR NOT NULL, 
	fan_id BIGINT NOT NULL, 
	kind VARCHAR(16) NOT NULL, 
	anchor_message_id BIGINT NOT NULL, 
	command_key VARCHAR(64) NOT NULL, 
	request_hash VARCHAR(64) NOT NULL, 
	snapshot_json TEXT NOT NULL, 
	snapshot_hash VARCHAR(64) NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	reason VARCHAR(64), 
	due_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	expires_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	intent_id VARCHAR(36), 
	cancel_requested_at TIMESTAMP WITHOUT TIME ZONE, 
	cancelled_by VARCHAR(128), 
	CONSTRAINT pk_of_engagement_action PRIMARY KEY (account_id, action_id), 
	CONSTRAINT fk_of_engagement_action_account_id_fans FOREIGN KEY(account_id, fan_id) REFERENCES fans (account_id, fan_id) ON DELETE RESTRICT, 
	CONSTRAINT fk_of_engagement_action_account_id_of_text_intent FOREIGN KEY(account_id, intent_id) REFERENCES of_text_intent (account_id, intent_id) ON DELETE RESTRICT, 
	CONSTRAINT uq_of_engagement_action_command UNIQUE (account_id, command_key), 
	CONSTRAINT uq_of_engagement_action_intent UNIQUE (account_id, intent_id), 
	CONSTRAINT uq_of_engagement_action_anchor UNIQUE (account_id, binding_id, fan_id, kind, anchor_message_id), 
	CONSTRAINT ck_of_engagement_action_kind CHECK (kind IN ('offer','follow_up','reengage')), 
	CONSTRAINT ck_of_engagement_action_status CHECK (status IN ('scheduled','queued','dispatching','accepted','not_accepted','unknown','cancelled','expired')), 
	CONSTRAINT ck_of_engagement_action_window CHECK (expires_at > due_at), 
	CONSTRAINT fk_of_engagement_action_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime (account_id) ON DELETE RESTRICT, 
	CONSTRAINT fk_of_engagement_action_binding_id_of_creator_bindings FOREIGN KEY(binding_id) REFERENCES of_creator_bindings (id) ON DELETE RESTRICT
);

CREATE INDEX ix_of_engagement_due ON of_engagement_action (account_id, status, due_at);

CREATE UNIQUE INDEX uq_of_engagement_active_fan ON of_engagement_action (account_id, fan_id) WHERE status IN ('scheduled','queued','dispatching','unknown');

-- History memory: evidence and corrections are append-only.
CREATE FUNCTION of_history_memory_evidence_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'historical memory evidence is append-only'; END;
$$;
CREATE TRIGGER of_history_memory_evidence_immutable BEFORE UPDATE OR DELETE ON of_history_memory_evidence
FOR EACH ROW EXECUTE FUNCTION of_history_memory_evidence_immutable();
CREATE FUNCTION of_history_memory_correction_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'historical memory corrections are append-only'; END;
$$;
CREATE TRIGGER of_history_memory_correction_immutable BEFORE UPDATE OR DELETE ON of_history_memory_correction
FOR EACH ROW EXECUTE FUNCTION of_history_memory_correction_immutable();

-- Personality: revision evidence is append-only.
CREATE FUNCTION of_personality_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'personality evidence is append-only'; END; $$;
CREATE TRIGGER of_personality_immutable BEFORE UPDATE OR DELETE ON of_personality_revision FOR EACH ROW EXECUTE FUNCTION of_personality_immutable();

-- Engagement: settings evidence is append-only; action definitions are frozen, terminal states stay terminal.
CREATE FUNCTION of_engagement_settings_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'engagement settings evidence is append-only'; END;
$$;
CREATE TRIGGER of_engagement_settings_immutable BEFORE UPDATE OR DELETE ON of_engagement_settings_change
  FOR EACH ROW EXECUTE FUNCTION of_engagement_settings_immutable();

CREATE FUNCTION of_engagement_action_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'engagement actions are retained'; END IF;
  IF ROW(NEW.account_id, NEW.action_id, NEW.binding_id, NEW.fan_id, NEW.kind,
         NEW.anchor_message_id, NEW.command_key, NEW.request_hash, NEW.snapshot_json,
         NEW.snapshot_hash, NEW.due_at, NEW.expires_at, NEW.created_at)
     IS DISTINCT FROM
     ROW(OLD.account_id, OLD.action_id, OLD.binding_id, OLD.fan_id, OLD.kind,
         OLD.anchor_message_id, OLD.command_key, OLD.request_hash, OLD.snapshot_json,
         OLD.snapshot_hash, OLD.due_at, OLD.expires_at, OLD.created_at) THEN
    RAISE EXCEPTION 'engagement definition is immutable';
  END IF;
  IF OLD.intent_id IS NOT NULL AND NEW.intent_id IS DISTINCT FROM OLD.intent_id THEN
    RAISE EXCEPTION 'engagement intent link is immutable';
  END IF;
  IF OLD.cancel_requested_at IS NOT NULL AND
    ROW(NEW.cancel_requested_at, NEW.cancelled_by) IS DISTINCT FROM ROW(OLD.cancel_requested_at, OLD.cancelled_by) THEN
    RAISE EXCEPTION 'engagement cancellation is retained';
  END IF;
  IF OLD.status IN ('accepted','not_accepted','cancelled','expired') AND NEW.status IS DISTINCT FROM OLD.status THEN
    RAISE EXCEPTION 'terminal engagement cannot be reopened';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER of_engagement_action_guard BEFORE UPDATE OR DELETE ON of_engagement_action
  FOR EACH ROW EXECUTE FUNCTION of_engagement_action_guard();

-- Ledger retry: dashboard commands that only make a held scan due; the command row is append-only evidence.
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
CREATE FUNCTION of_ledger_retry_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'ledger retry evidence is append-only'; END; $$;
CREATE TRIGGER of_ledger_retry_immutable BEFORE UPDATE OR DELETE ON of_ledger_retry_command FOR EACH ROW EXECUTE FUNCTION of_ledger_retry_immutable();

-- Content-sync review fixes (additive; same unpublished launch revision).
-- A source returning to an earlier state is a new sighting, so the observation identity gains an occurrence.
ALTER TABLE of_offer_observation ADD COLUMN occurrence INTEGER DEFAULT 0 NOT NULL;
ALTER TABLE of_offer_observation DROP CONSTRAINT uq_of_offer_observation_source;
ALTER TABLE of_offer_observation ADD CONSTRAINT uq_of_offer_observation_sighting UNIQUE (account_id, binding_id, source_kind, source_id, fingerprint, occurrence);

ALTER TABLE of_content_sync_account ADD COLUMN posts_cursor VARCHAR(64);
ALTER TABLE of_content_sync_account ADD COLUMN posts_history_complete BOOLEAN DEFAULT false NOT NULL;
ALTER TABLE of_content_sync_account ADD COLUMN posts_audit_after TIMESTAMP WITHOUT TIME ZONE;
ALTER TABLE of_content_sync_account ADD COLUMN late_through_at TIMESTAMP WITHOUT TIME ZONE;
ALTER TABLE of_content_sync_account ADD COLUMN reconcile_priority_after_item_id INTEGER;

CREATE TABLE of_content_sync_source (
	account_id VARCHAR NOT NULL, 
	binding_id VARCHAR NOT NULL, 
	fan_id BIGINT NOT NULL, 
	message_id BIGINT NOT NULL, 
	refresh_pending BOOLEAN NOT NULL, 
	refresh_attempts INTEGER NOT NULL, 
	folded_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	CONSTRAINT pk_of_content_sync_source PRIMARY KEY (account_id, binding_id, fan_id, message_id), 
	CONSTRAINT ck_of_content_sync_source_attempts CHECK (refresh_attempts >= 0), 
	CONSTRAINT fk_of_content_sync_source_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime (account_id) ON DELETE RESTRICT, 
	CONSTRAINT fk_of_content_sync_source_binding_id_of_creator_bindings FOREIGN KEY(binding_id) REFERENCES of_creator_bindings (id) ON DELETE RESTRICT
);
CREATE INDEX ix_of_content_sync_source_refresh ON of_content_sync_source (account_id, binding_id, message_id) WHERE refresh_pending;

CREATE TABLE of_provider_read_gate (
	account_id VARCHAR NOT NULL, 
	holder VARCHAR(32), 
	lease_token VARCHAR(36), 
	lease_until TIMESTAMP WITHOUT TIME ZONE, 
	next_read_at TIMESTAMP WITHOUT TIME ZONE, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	CONSTRAINT pk_of_provider_read_gate PRIMARY KEY (account_id), 
	CONSTRAINT ck_of_provider_read_gate_lease CHECK ((lease_token IS NULL) = (lease_until IS NULL)), 
	CONSTRAINT fk_of_provider_read_gate_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime (account_id) ON DELETE RESTRICT
);

-- Late-import detection reads recently ingested outgoing rows per account.
CREATE INDEX ix_messages_account_ingested ON messages (account_id, ingested_at);
