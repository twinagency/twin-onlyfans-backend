-- Frozen 0071 foundation DDL. No history/control/receipt rewrites.
CREATE TABLE of_runtime (
	account_id VARCHAR NOT NULL,
	binding_id VARCHAR NOT NULL,
	enabled BOOLEAN DEFAULT false NOT NULL,
	activated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	last_claim_at TIMESTAMP WITHOUT TIME ZONE DEFAULT '1970-01-01' NOT NULL,
	recovery_needed BOOLEAN DEFAULT true NOT NULL,
	last_sync_at TIMESTAMP WITHOUT TIME ZONE,
	pending_events INTEGER DEFAULT 0 NOT NULL,
	CONSTRAINT pk_of_runtime PRIMARY KEY (account_id),
	CONSTRAINT ck_of_runtime_ck_of_runtime_pending CHECK (pending_events >= 0 AND pending_events <= 1000),
	CONSTRAINT fk_of_runtime_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE RESTRICT,
	CONSTRAINT fk_of_runtime_binding_id_of_creator_bindings FOREIGN KEY(binding_id) REFERENCES of_creator_bindings (id) ON DELETE RESTRICT
);

CREATE INDEX ix_of_runtime_fair ON of_runtime (enabled, last_claim_at, account_id);
CREATE TABLE of_control_audit (
	account_id VARCHAR NOT NULL,
	command_key VARCHAR(64) NOT NULL,
	fan_id BIGINT NOT NULL,
	mode VARCHAR(24) NOT NULL,
	previous_revision INTEGER NOT NULL,
	revision INTEGER NOT NULL,
	actor VARCHAR(128) NOT NULL,
	reason VARCHAR(512) NOT NULL,
	consent_ref VARCHAR(128),
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	CONSTRAINT pk_of_control_audit PRIMARY KEY (account_id, command_key),
	CONSTRAINT fk_of_control_audit_account_id_fans FOREIGN KEY(account_id, fan_id) REFERENCES fans (account_id, fan_id) ON DELETE RESTRICT,
	CONSTRAINT fk_of_control_audit_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime (account_id) ON DELETE RESTRICT
);

CREATE TABLE of_ingress (
	account_id VARCHAR NOT NULL,
	event_key VARCHAR(64) NOT NULL,
	binding_id VARCHAR NOT NULL,
	fan_id BIGINT NOT NULL,
	message_id BIGINT NOT NULL,
	source VARCHAR(16) NOT NULL,
	payload TEXT NOT NULL,
	status VARCHAR(16) DEFAULT 'pending' NOT NULL,
	reason VARCHAR(64),
	received_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	applied_at TIMESTAMP WITHOUT TIME ZONE,
	CONSTRAINT pk_of_ingress PRIMARY KEY (account_id, event_key),
	CONSTRAINT ck_of_ingress_ck_of_ingress_status CHECK (status IN ('pending','applied','quarantined')),
	CONSTRAINT fk_of_ingress_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime (account_id) ON DELETE RESTRICT,
	CONSTRAINT fk_of_ingress_binding_id_of_creator_bindings FOREIGN KEY(binding_id) REFERENCES of_creator_bindings (id) ON DELETE RESTRICT
);

CREATE INDEX ix_of_ingress_pending ON of_ingress (account_id, status, received_at, event_key);
CREATE TABLE of_recovery (
	account_id VARCHAR NOT NULL,
	fan_id BIGINT NOT NULL,
	high_water BIGINT DEFAULT 0 NOT NULL,
	upper_bound BIGINT,
	before_id BIGINT,
	generation INTEGER DEFAULT 0 NOT NULL,
	status VARCHAR(16) DEFAULT 'needed' NOT NULL,
	lease_token VARCHAR(36),
	lease_until TIMESTAMP WITHOUT TIME ZONE,
	last_complete_at TIMESTAMP WITHOUT TIME ZONE,
	CONSTRAINT pk_of_recovery PRIMARY KEY (account_id, fan_id),
	CONSTRAINT ck_of_recovery_ck_of_recovery_status CHECK (status IN ('needed','running','complete')),
	CONSTRAINT fk_of_recovery_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime (account_id) ON DELETE RESTRICT
);

CREATE INDEX ix_of_recovery_due ON of_recovery (account_id, status, lease_until, fan_id);
CREATE TABLE of_scan (
	account_id VARCHAR NOT NULL,
	generation INTEGER DEFAULT 0 NOT NULL,
	cursor VARCHAR(1024),
	status VARCHAR(16) DEFAULT 'needed' NOT NULL,
	page_count INTEGER DEFAULT 0 NOT NULL,
	known_fan_after BIGINT DEFAULT 0 NOT NULL,
	known_fans_complete BOOLEAN DEFAULT false NOT NULL,
	repeat_requested BOOLEAN DEFAULT false NOT NULL,
	lease_token VARCHAR(36),
	lease_until TIMESTAMP WITHOUT TIME ZONE,
	CONSTRAINT pk_of_scan PRIMARY KEY (account_id),
	CONSTRAINT ck_of_scan_ck_of_scan_status CHECK (status IN ('needed','running','complete')),
	CONSTRAINT fk_of_scan_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime (account_id) ON DELETE RESTRICT
);

CREATE TABLE of_work (
	account_id VARCHAR NOT NULL,
	fan_id BIGINT NOT NULL,
	generation INTEGER DEFAULT 0 NOT NULL,
	status VARCHAR(16) NOT NULL,
	first_input_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	last_input_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	due_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	target_message_id BIGINT NOT NULL,
	control_revision INTEGER NOT NULL,
	lease_token VARCHAR(36),
	lease_until TIMESTAMP WITHOUT TIME ZONE,
	last_claim_at TIMESTAMP WITHOUT TIME ZONE DEFAULT '1970-01-01' NOT NULL,
	CONSTRAINT pk_of_work PRIMARY KEY (account_id, fan_id),
	CONSTRAINT fk_of_work_account_id_fans FOREIGN KEY(account_id, fan_id) REFERENCES fans (account_id, fan_id) ON DELETE RESTRICT,
	CONSTRAINT ck_of_work_ck_of_work_status CHECK (status IN ('waiting','claimed','blocked','done')),
	CONSTRAINT fk_of_work_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime (account_id) ON DELETE RESTRICT
);

CREATE INDEX ix_of_work_due ON of_work (account_id, status, due_at, last_claim_at, fan_id);
