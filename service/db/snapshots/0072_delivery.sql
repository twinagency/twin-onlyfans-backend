-- Frozen synthetic-only 0072; no activation, policy reset, or destructive DDL.

ALTER TABLE of_conversation_state ADD COLUMN input_revision BIGINT DEFAULT 0 NOT NULL;

ALTER TABLE of_message_delivery ADD COLUMN origin VARCHAR;

CREATE TABLE of_delivery_policy (
    account_id VARCHAR NOT NULL,
    revision INTEGER DEFAULT 0 NOT NULL,
    enabled BOOLEAN DEFAULT false NOT NULL,
    transport_kind VARCHAR(16) DEFAULT 'synthetic' NOT NULL,
    limit_units INTEGER DEFAULT 0 NOT NULL,
    used_units INTEGER DEFAULT 0 NOT NULL,
    queued_count INTEGER DEFAULT 0 NOT NULL,
    throttle_seconds INTEGER DEFAULT 1 NOT NULL,
    next_dispatch_at TIMESTAMP WITHOUT TIME ZONE,
    active_intent_id VARCHAR(36),
    CONSTRAINT pk_of_delivery_policy PRIMARY KEY (account_id),
    CONSTRAINT ck_of_delivery_policy_transport_kind CHECK (transport_kind = 'synthetic'),
    CONSTRAINT ck_of_delivery_policy_limit_units CHECK (limit_units >= 0 AND limit_units <= 10000),
    CONSTRAINT ck_of_delivery_policy_used_units CHECK (used_units >= 0),
    CONSTRAINT ck_of_delivery_policy_queued_count CHECK (queued_count >= 0 AND queued_count <= 1000),
    CONSTRAINT ck_of_delivery_policy_throttle_seconds CHECK (throttle_seconds >= 1 AND throttle_seconds <= 86400),
    CONSTRAINT fk_of_delivery_policy_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime (account_id) ON DELETE RESTRICT
);

CREATE TABLE of_delivery_policy_change (
    account_id VARCHAR NOT NULL,
    revision INTEGER NOT NULL,
    actor VARCHAR(128) NOT NULL,
    reason VARCHAR(512) NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    CONSTRAINT pk_of_delivery_policy_change PRIMARY KEY (account_id, revision),
    CONSTRAINT fk_of_delivery_policy_change_account_id_of_delivery_policy FOREIGN KEY(account_id) REFERENCES of_delivery_policy (account_id) ON DELETE RESTRICT
);

CREATE TABLE of_text_intent (
    account_id VARCHAR NOT NULL,
    intent_id VARCHAR(36) NOT NULL,
    fan_id BIGINT NOT NULL,
    binding_id VARCHAR NOT NULL,
    command_key VARCHAR(64) NOT NULL,
    actor VARCHAR(128) NOT NULL,
    origin VARCHAR(16) NOT NULL,
    body TEXT NOT NULL,
    command_hash VARCHAR(64) NOT NULL,
    payload_hash VARCHAR(64) NOT NULL,
    control_revision INTEGER NOT NULL,
    input_revision BIGINT NOT NULL,
    policy_revision INTEGER NOT NULL,
    work_generation INTEGER,
    work_token VARCHAR(36),
    status VARCHAR(24) NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    expires_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    attempt_id VARCHAR(36),
    dispatch_token VARCHAR(36),
    dispatched_at TIMESTAMP WITHOUT TIME ZONE,
    deadline TIMESTAMP WITHOUT TIME ZONE,
    provider_message_id BIGINT,
    receipt_json TEXT,
    evidence_ref VARCHAR(256),
    reason VARCHAR(128),
    projected_at TIMESTAMP WITHOUT TIME ZONE,
    reservation_held BOOLEAN DEFAULT false NOT NULL,
    CONSTRAINT pk_of_text_intent PRIMARY KEY (account_id, intent_id),
    CONSTRAINT fk_of_text_intent_account_id_fans FOREIGN KEY(account_id, fan_id) REFERENCES fans (account_id, fan_id) ON DELETE RESTRICT,
    CONSTRAINT uq_of_text_intent_command UNIQUE (account_id, command_key),
    CONSTRAINT uq_of_text_intent_attempt UNIQUE (account_id, attempt_id),
    CONSTRAINT uq_of_text_intent_provider_message UNIQUE (account_id, provider_message_id),
    CONSTRAINT ck_of_text_intent_origin CHECK (origin IN ('automated','manual')),
    CONSTRAINT ck_of_text_intent_status CHECK (status IN ('queued','dispatching','accepted','not_accepted','unknown','cancelled','refused')),
    CONSTRAINT ck_of_text_intent_provider_message_id CHECK (provider_message_id IS NULL OR provider_message_id > 0),
    CONSTRAINT fk_of_text_intent_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime (account_id) ON DELETE RESTRICT,
    CONSTRAINT fk_of_text_intent_binding_id_of_creator_bindings FOREIGN KEY(binding_id) REFERENCES of_creator_bindings (id) ON DELETE RESTRICT
);

CREATE INDEX ix_of_text_intent_queued ON of_text_intent (account_id, status, created_at, intent_id);

CREATE INDEX ix_of_text_intent_unresolved ON of_text_intent (account_id, status, deadline) WHERE status IN ('dispatching','unknown');

CREATE INDEX ix_of_message_delivery_unresolved ON of_message_delivery (account_id, fan_id, outcome) WHERE outcome IN ('generating','queued','dispatching','unknown','partial');

CREATE INDEX ix_messages_account_provider_id ON messages (account_id, message_id);
