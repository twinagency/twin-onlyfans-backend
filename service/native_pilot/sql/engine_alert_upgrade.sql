-- FASTT throughput work: one additive table in the runtime schema. Idempotent. Nothing in the application (public)
-- schema changes; the previous images ignore it. The engine creates the same table itself on start
-- (store.ensure_runtime_schema); this file is the reviewable / manual equivalent.
CREATE SCHEMA IF NOT EXISTS fastt_runtime;

CREATE TABLE IF NOT EXISTS fastt_runtime.engine_alert (
    account_id  VARCHAR NOT NULL,
    kind        VARCHAR NOT NULL,
    state       VARCHAR NOT NULL,
    first_at    VARCHAR NOT NULL,
    last_at     VARCHAR NOT NULL,
    count       INTEGER NOT NULL DEFAULT 1,
    detail_json TEXT,
    PRIMARY KEY (account_id, kind)
);
