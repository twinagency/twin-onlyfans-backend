-- Existing configuration, holds and consumed allowances remain authoritative.
ALTER TABLE of_runtime ADD COLUMN configuration_revision INTEGER NOT NULL DEFAULT 0;
CREATE TABLE of_configuration_change (
 account_id VARCHAR NOT NULL REFERENCES of_runtime(account_id) ON DELETE RESTRICT,
 command_key VARCHAR(64) NOT NULL,
 previous_revision INTEGER NOT NULL,
 revision INTEGER NOT NULL,
 fingerprint_before VARCHAR(64) NOT NULL,
 fingerprint_after VARCHAR(64) NOT NULL,
 request_hash VARCHAR(64) NOT NULL,
 actor VARCHAR(128) NOT NULL,
 before_json TEXT NOT NULL,
 after_json TEXT NOT NULL,
 created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
 PRIMARY KEY(account_id,command_key)
);
CREATE FUNCTION of_configuration_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'configuration evidence is append-only';
END;
$$;
CREATE TRIGGER of_configuration_immutable BEFORE UPDATE OR DELETE ON of_configuration_change
FOR EACH ROW EXECUTE FUNCTION of_configuration_immutable();
