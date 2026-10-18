-- Provenance only; Fan/FanProfile remain the sole current-value owners.
CREATE TABLE of_native_memory_change (
    account_id VARCHAR NOT NULL,
    change_id VARCHAR(36) NOT NULL,
    fan_id BIGINT NOT NULL,
    plan_id VARCHAR(36) NOT NULL,
    source_message_id BIGINT NOT NULL,
    input_revision BIGINT NOT NULL,
    target VARCHAR(16) NOT NULL,
    field VARCHAR(32) NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    source_quote TEXT NOT NULL,
    context_hash VARCHAR(64) NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    PRIMARY KEY (account_id, change_id),
    FOREIGN KEY (account_id, fan_id) REFERENCES fans(account_id, fan_id) ON DELETE RESTRICT,
    FOREIGN KEY (account_id, plan_id) REFERENCES of_native_plan(account_id, plan_id) ON DELETE RESTRICT,
    FOREIGN KEY (account_id, fan_id, source_message_id) REFERENCES messages(account_id, fan_id, message_id) ON DELETE RESTRICT,
    CONSTRAINT uq_of_native_memory_plan_field UNIQUE (account_id, plan_id, target, field),
    CONSTRAINT ck_of_native_memory_change_target CHECK (target IN ('fan','profile'))
);
CREATE INDEX ix_of_native_memory_fan ON of_native_memory_change(account_id, fan_id, created_at);
CREATE FUNCTION of_native_memory_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'native memory evidence is append-only';
END;
$$;
CREATE TRIGGER of_native_memory_immutable BEFORE UPDATE OR DELETE ON of_native_memory_change
FOR EACH ROW EXECUTE FUNCTION of_native_memory_immutable();
