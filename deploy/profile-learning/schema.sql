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
    checked_at timestamp,
    error_code text,
    PRIMARY KEY (tenant_id, account_id, binding_id, fan_id)
);
CREATE INDEX IF NOT EXISTS ix_of_profile_learning_due ON of_profile_learning(tenant_id,next_at);
-- Current-binding evidence lookup for the history scanner. Run during the
-- reviewed deployment window; a busy production table may need CONCURRENTLY.
CREATE INDEX IF NOT EXISTS ix_of_ingress_profile_source
    ON of_ingress(account_id,binding_id,fan_id,message_id) WHERE status='applied';
