-- Preserve the native catalogue, media identifiers, financial state and holds.
CREATE TABLE of_content_change (
 account_id VARCHAR NOT NULL REFERENCES of_runtime(account_id) ON DELETE RESTRICT,
 item_id INTEGER NOT NULL REFERENCES catalog_items(id) ON DELETE RESTRICT,
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
 PRIMARY KEY(account_id,item_id,command_key),
 CONSTRAINT uq_of_content_revision UNIQUE(account_id,item_id,revision),
 CONSTRAINT ck_of_content_change_revision CHECK(previous_revision >= 0 AND revision = previous_revision + 1)
);
CREATE FUNCTION of_content_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'content evidence is append-only';
END;
$$;
CREATE TRIGGER of_content_immutable BEFORE UPDATE OR DELETE ON of_content_change
FOR EACH ROW EXECUTE FUNCTION of_content_immutable();
