-- Concurrent delivery state, additive in fastt_runtime. Legacy mode does not read these tables.
CREATE SCHEMA IF NOT EXISTS fastt_runtime;

CREATE TABLE IF NOT EXISTS fastt_runtime.engine_conversation_lease (
    tenant_id   VARCHAR NOT NULL,
    account_id  VARCHAR NOT NULL,
    fan_id      BIGINT NOT NULL,
    holder      VARCHAR NOT NULL,
    owner_token VARCHAR NOT NULL,
    attempt_id  VARCHAR,
    state       VARCHAR NOT NULL,
    acquired_at VARCHAR NOT NULL,
    heartbeat_at VARCHAR NOT NULL,
    expires_at  VARCHAR NOT NULL,
    next_send_at VARCHAR,
    updated_at  VARCHAR NOT NULL,
    PRIMARY KEY (tenant_id, account_id, fan_id)
);

CREATE TABLE IF NOT EXISTS fastt_runtime.engine_dispatch_slot (
    slot_id     VARCHAR NOT NULL PRIMARY KEY,
    tenant_id   VARCHAR NOT NULL,
    account_id  VARCHAR NOT NULL,
    fan_id      BIGINT NOT NULL,
    attempt_id  VARCHAR,
    state       VARCHAR NOT NULL,
    admitted_at VARCHAR NOT NULL,
    expires_at  VARCHAR NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_engine_dispatch_slot_account
    ON fastt_runtime.engine_dispatch_slot (account_id, state, expires_at);

CREATE TABLE IF NOT EXISTS fastt_runtime.engine_provider_bucket (
    account_id VARCHAR NOT NULL PRIMARY KEY,
    tokens     VARCHAR NOT NULL,
    updated_at VARCHAR NOT NULL
);
