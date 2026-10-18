CREATE TABLE of_native_plan (
    account_id VARCHAR NOT NULL REFERENCES of_runtime(account_id) ON DELETE RESTRICT,
    plan_id VARCHAR(36) NOT NULL,
    fan_id BIGINT NOT NULL,
    binding_id VARCHAR NOT NULL REFERENCES of_creator_bindings(id) ON DELETE RESTRICT,
    work_generation INTEGER NOT NULL,
    input_revision BIGINT NOT NULL,
    claim_json TEXT NOT NULL,
    context_json TEXT NOT NULL,
    context_hash VARCHAR(64) NOT NULL,
    planner_version VARCHAR(32) NOT NULL,
    model_id VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL,
    reason VARCHAR(64),
    body TEXT,
    intent_id VARCHAR(36),
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    expires_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    PRIMARY KEY (account_id,plan_id),
    FOREIGN KEY (account_id,fan_id) REFERENCES fans(account_id,fan_id) ON DELETE RESTRICT,
    CONSTRAINT uq_of_native_plan_input UNIQUE (account_id,fan_id,input_revision),
    CONSTRAINT ck_of_native_plan_status CHECK (status IN ('generating','ready','queued','stale','unknown','refused'))
);
CREATE INDEX ix_of_native_plan_state ON of_native_plan(account_id,status,created_at);
ALTER TABLE of_text_intent ADD COLUMN native_plan_id VARCHAR(36);
ALTER TABLE of_text_intent ADD CONSTRAINT fk_of_text_intent_native_plan
    FOREIGN KEY (account_id,native_plan_id) REFERENCES of_native_plan(account_id,plan_id) ON DELETE RESTRICT;
