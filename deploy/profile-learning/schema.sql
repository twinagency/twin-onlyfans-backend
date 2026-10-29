-- Additive integration DDL. Apply explicitly after checking the running release.
-- No runtime create_all or alteration of existing sender/control tables.
CREATE TABLE IF NOT EXISTS of_profile_learning (
    tenant_id text NOT NULL,
    account_id text NOT NULL REFERENCES of_runtime(account_id) ON DELETE RESTRICT,
    binding_id text NOT NULL REFERENCES of_creator_bindings(id) ON DELETE RESTRICT,
    fan_id bigint NOT NULL CHECK (fan_id >= 0), -- zero is the creator draft
    document_json text NOT NULL DEFAULT '{}',
    cursor_at timestamp NOT NULL DEFAULT '1970-01-01',
    cursor_message bigint NOT NULL DEFAULT 0,
    cursor_fan bigint NOT NULL DEFAULT 0,
    processed_messages bigint NOT NULL DEFAULT 0,
    next_at timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
    queued_at timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_completed_at timestamp,
    checked_at timestamp,
    error_code text,
    lease_token text,
    lease_until timestamp,
    attempts integer NOT NULL DEFAULT 0,
    maintenance_at timestamp,
    PRIMARY KEY (tenant_id, account_id, binding_id, fan_id)
);
CREATE INDEX IF NOT EXISTS ix_of_profile_learning_due ON of_profile_learning(tenant_id,next_at);
-- Current-binding evidence lookup for the history scanner. Run during the
-- reviewed deployment window; a busy production table may need CONCURRENTLY.
CREATE INDEX IF NOT EXISTS ix_of_ingress_profile_source
    ON of_ingress(account_id,binding_id,fan_id,message_id) WHERE status='applied';

-- V2 receipts checkpoint every chunk, including edits to previously uninteresting messages.
CREATE TABLE IF NOT EXISTS of_profile_receipts (
    tenant_id text NOT NULL, account_id text NOT NULL, binding_id text NOT NULL,
    fan_id bigint NOT NULL, source_fan bigint NOT NULL, message_id bigint NOT NULL,
    body_md5 text NOT NULL, source_at timestamp NOT NULL, direction text NOT NULL,
    unsent boolean NOT NULL, automation text NOT NULL DEFAULT '',
    next_chunk integer NOT NULL DEFAULT 0, total_chunks integer NOT NULL,
    processed_at timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(tenant_id,account_id,binding_id,fan_id,source_fan,message_id),
    FOREIGN KEY(tenant_id,account_id,binding_id,fan_id)
        REFERENCES of_profile_learning(tenant_id,account_id,binding_id,fan_id) ON DELETE CASCADE,
    CHECK (next_chunk >= 0 AND next_chunk <= total_chunks)
);
CREATE TABLE IF NOT EXISTS of_profile_observations (
    tenant_id text NOT NULL, account_id text NOT NULL, binding_id text NOT NULL,
    fan_id bigint NOT NULL, observation_id text NOT NULL,
    source_fan bigint NOT NULL, message_id bigint NOT NULL, document_json text NOT NULL,
    PRIMARY KEY(tenant_id,account_id,binding_id,fan_id,observation_id),
    FOREIGN KEY(tenant_id,account_id,binding_id,fan_id)
        REFERENCES of_profile_learning(tenant_id,account_id,binding_id,fan_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_profile_observation_source
    ON of_profile_observations(account_id,binding_id,source_fan,message_id);
CREATE TABLE IF NOT EXISTS of_profile_memories (
    tenant_id text NOT NULL, account_id text NOT NULL, binding_id text NOT NULL,
    fan_id bigint NOT NULL, memory_key text NOT NULL, field text NOT NULL,
    status text NOT NULL, kind text NOT NULL, document_json text NOT NULL,
    PRIMARY KEY(tenant_id,account_id,binding_id,fan_id,memory_key),
    FOREIGN KEY(tenant_id,account_id,binding_id,fan_id)
        REFERENCES of_profile_learning(tenant_id,account_id,binding_id,fan_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS of_profile_manual (
    tenant_id text NOT NULL, account_id text NOT NULL, binding_id text NOT NULL,
    fan_id bigint NOT NULL, memory_key text NOT NULL, revision integer NOT NULL,
    document_json text NOT NULL, protected boolean NOT NULL,
    PRIMARY KEY(tenant_id,account_id,binding_id,fan_id,memory_key),
    FOREIGN KEY(tenant_id,account_id,binding_id,fan_id)
        REFERENCES of_profile_learning(tenant_id,account_id,binding_id,fan_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS of_profile_manual_changes (
    tenant_id text NOT NULL, account_id text NOT NULL, binding_id text NOT NULL,
    fan_id bigint NOT NULL, command_key text NOT NULL, memory_key text NOT NULL,
    revision integer NOT NULL, request_hash text NOT NULL, actor text NOT NULL,
    reason text NOT NULL, document_json text NOT NULL, created_at timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(tenant_id,account_id,binding_id,fan_id,command_key),
    FOREIGN KEY(tenant_id,account_id,binding_id,fan_id)
        REFERENCES of_profile_learning(tenant_id,account_id,binding_id,fan_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS of_profile_accounts (
    tenant_id text NOT NULL,account_id text NOT NULL,binding_id text NOT NULL,
    last_claimed_at timestamp NOT NULL DEFAULT '1970-01-01',
    PRIMARY KEY(tenant_id,account_id,binding_id)
);
