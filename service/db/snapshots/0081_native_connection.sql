ALTER TABLE of_delivery_policy DROP CONSTRAINT ck_of_delivery_policy_transport_kind;
ALTER TABLE of_delivery_policy ADD CONSTRAINT ck_of_delivery_policy_transport_kind CHECK (transport_kind IN ('synthetic','native'));
CREATE TABLE of_provider_connection (
 account_id VARCHAR PRIMARY KEY REFERENCES of_runtime(account_id) ON DELETE RESTRICT,
 binding_id VARCHAR NOT NULL REFERENCES of_creator_bindings(id) ON DELETE RESTRICT,
 owner_id BIGINT NOT NULL, revision INTEGER NOT NULL, sealed_config TEXT NOT NULL,
 expires_at TIMESTAMP NOT NULL, status VARCHAR(16) NOT NULL,
 verified_at TIMESTAMP, last_error VARCHAR(64), updated_at TIMESTAMP NOT NULL,
 automate_new_conversations BOOLEAN NOT NULL DEFAULT false, automation_starts_at TIMESTAMP,
 CONSTRAINT ck_of_provider_connection_status CHECK (status IN ('pending','verified','failed','expired','paused'))
);
CREATE TABLE of_connection_change (
 account_id VARCHAR NOT NULL REFERENCES of_runtime(account_id) ON DELETE RESTRICT,
 command_key VARCHAR(64) NOT NULL, revision INTEGER NOT NULL, action VARCHAR(32) NOT NULL,
 request_hash VARCHAR(64) NOT NULL, actor VARCHAR(128) NOT NULL, created_at TIMESTAMP NOT NULL,
 PRIMARY KEY(account_id,command_key)
);
CREATE FUNCTION of_connection_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'connection evidence is append-only'; END; $$;
CREATE TRIGGER of_connection_immutable BEFORE UPDATE OR DELETE ON of_connection_change FOR EACH ROW EXECUTE FUNCTION of_connection_immutable();
