-- FASTT engine v2: two additive tables in the runtime schema. Idempotent. Nothing in the
-- application (public) schema changes; the previous images ignore these tables.
-- The engine creates the same tables itself on start (store.ensure_runtime_schema); this file is
-- the reviewable / manual equivalent.
CREATE SCHEMA IF NOT EXISTS fastt_runtime;

CREATE TABLE IF NOT EXISTS fastt_runtime.engine_runner_lease (
    account_id   VARCHAR NOT NULL PRIMARY KEY,
    owner_token  VARCHAR NOT NULL,
    holder       VARCHAR NOT NULL,
    generation   INTEGER NOT NULL DEFAULT 1,
    acquired_at  VARCHAR NOT NULL,
    heartbeat_at VARCHAR NOT NULL,
    expires_at   VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS fastt_runtime.engine_work (
    account_id    VARCHAR NOT NULL,
    fan_id        BIGINT NOT NULL,
    state         VARCHAR NOT NULL,
    reason        VARCHAR,
    message_id    BIGINT,
    waiting_since VARCHAR,
    state_since   VARCHAR NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    updated_at    VARCHAR NOT NULL,
    PRIMARY KEY (account_id, fan_id)
);
CREATE INDEX IF NOT EXISTS ix_engine_work_account_state ON fastt_runtime.engine_work (account_id, state);
