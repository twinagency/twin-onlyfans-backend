CREATE TABLE of_ledger_history (
 account_id VARCHAR PRIMARY KEY REFERENCES of_runtime(account_id) ON DELETE RESTRICT,
 binding_id VARCHAR NOT NULL REFERENCES of_creator_bindings(id) ON DELETE RESTRICT,
 connection_revision INTEGER NOT NULL,
 owner_id BIGINT NOT NULL,
 window_start TIMESTAMP WITHOUT TIME ZONE NOT NULL,
 window_end TIMESTAMP WITHOUT TIME ZONE NOT NULL,
 page_offset INTEGER NOT NULL,
 status VARCHAR(16) NOT NULL,
 lease_token VARCHAR(36),
 lease_until TIMESTAMP WITHOUT TIME ZONE,
 next_poll_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
 last_page_hash VARCHAR(64),
 last_complete_at TIMESTAMP WITHOUT TIME ZONE,
 last_full_at TIMESTAMP WITHOUT TIME ZONE,
 last_error VARCHAR(64),
 updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
 CONSTRAINT ck_of_ledger_history_offset CHECK(page_offset >= 0 AND page_offset <= 1000000000),
 CONSTRAINT ck_of_ledger_history_status CHECK(status IN ('idle','running','held')),
 CONSTRAINT ck_of_ledger_history_lease CHECK((lease_token IS NULL) = (lease_until IS NULL))
);
CREATE INDEX ix_of_ledger_history_due ON of_ledger_history(next_poll_at,account_id);
