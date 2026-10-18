CREATE TABLE of_vault_edit (
 account_id VARCHAR NOT NULL REFERENCES of_runtime(account_id) ON DELETE RESTRICT,
 command_key VARCHAR(64) NOT NULL,
 binding_id VARCHAR NOT NULL REFERENCES of_creator_bindings(id) ON DELETE RESTRICT,
 request_hash VARCHAR(64) NOT NULL, actor VARCHAR(128) NOT NULL,
 operation VARCHAR(24) NOT NULL, target_id VARCHAR(24) NOT NULL,
 before_json TEXT NOT NULL, after_json TEXT NOT NULL, result_json TEXT NOT NULL,
 created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
 PRIMARY KEY(account_id,command_key)
);
CREATE FUNCTION of_vault_edit_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'vault edit evidence is append-only'; END;
$$;
CREATE TRIGGER of_vault_edit_immutable BEFORE UPDATE OR DELETE ON of_vault_edit
FOR EACH ROW EXECUTE FUNCTION of_vault_edit_immutable();
