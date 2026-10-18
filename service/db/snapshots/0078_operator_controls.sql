-- Retain every old command, revision, hold and consumed allowance unchanged.
ALTER TABLE of_control_audit ADD COLUMN action VARCHAR(32);
ALTER TABLE of_control_audit ADD COLUMN takeover_from VARCHAR(16);
ALTER TABLE of_control_audit ADD COLUMN takeover_to VARCHAR(16);
CREATE FUNCTION of_control_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'control evidence is append-only';
END;
$$;
CREATE TRIGGER of_control_immutable BEFORE UPDATE OR DELETE ON of_control_audit
FOR EACH ROW EXECUTE FUNCTION of_control_immutable();
