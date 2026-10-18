CREATE TABLE of_creator_facts_source (
 account_id VARCHAR PRIMARY KEY REFERENCES of_runtime(account_id) ON DELETE RESTRICT,
 binding_id VARCHAR NOT NULL REFERENCES of_creator_bindings(id) ON DELETE RESTRICT,
 source_json TEXT,
 source_hash VARCHAR(64),
 source_updated_at TIMESTAMP WITHOUT TIME ZONE,
 overrides_json TEXT NOT NULL,
 checked_at TIMESTAMP WITHOUT TIME ZONE,
 attempted_at TIMESTAMP WITHOUT TIME ZONE,
 refresh_token VARCHAR(36),
 refresh_until TIMESTAMP WITHOUT TIME ZONE,
 last_error VARCHAR(64),
 CONSTRAINT ck_of_creator_facts_source_lease CHECK ((refresh_token IS NULL) = (refresh_until IS NULL))
);
