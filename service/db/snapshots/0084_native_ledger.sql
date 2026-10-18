CREATE TABLE of_ledger_scan (
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
 CONSTRAINT ck_of_ledger_scan_offset CHECK(page_offset >= 0 AND page_offset <= 10000),
 CONSTRAINT ck_of_ledger_scan_status CHECK(status IN ('idle','running','held')),
 CONSTRAINT ck_of_ledger_scan_lease CHECK((lease_token IS NULL) = (lease_until IS NULL))
);
CREATE INDEX ix_of_ledger_due ON of_ledger_scan(next_poll_at,account_id);
CREATE TABLE of_ledger_evidence (
 account_id VARCHAR NOT NULL REFERENCES of_runtime(account_id) ON DELETE RESTRICT,
 evidence_hash VARCHAR(64) NOT NULL,
 provider_transaction_id VARCHAR(64) NOT NULL,
 fan_id BIGINT,
 transaction_id INTEGER REFERENCES transactions(id) ON DELETE RESTRICT,
 outcome VARCHAR(16) NOT NULL,
 observation_json TEXT NOT NULL,
 recorded_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
 PRIMARY KEY(account_id,evidence_hash),
 CONSTRAINT ck_of_ledger_evidence_outcome CHECK(outcome IN ('applied','stale','conflict'))
);
CREATE INDEX ix_of_ledger_evidence_provider ON of_ledger_evidence(account_id,provider_transaction_id);
CREATE FUNCTION of_ledger_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 RAISE EXCEPTION 'ledger evidence is append-only';
END;
$$;
CREATE TRIGGER of_ledger_immutable BEFORE UPDATE OR DELETE ON of_ledger_evidence
FOR EACH ROW EXECUTE FUNCTION of_ledger_immutable();
