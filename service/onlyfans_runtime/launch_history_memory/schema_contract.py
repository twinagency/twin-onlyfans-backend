"""Append-only guards shipped by 0090_launch (db/snapshots/0090_launch.sql); importing never executes DDL."""
EVIDENCE_GUARDS_SQL = """
CREATE FUNCTION of_history_memory_evidence_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'historical memory evidence is append-only'; END;
$$;
CREATE TRIGGER of_history_memory_evidence_immutable BEFORE UPDATE OR DELETE ON of_history_memory_evidence
FOR EACH ROW EXECUTE FUNCTION of_history_memory_evidence_immutable();
CREATE FUNCTION of_history_memory_correction_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'historical memory corrections are append-only'; END;
$$;
CREATE TRIGGER of_history_memory_correction_immutable BEFORE UPDATE OR DELETE ON of_history_memory_correction
FOR EACH ROW EXECUTE FUNCTION of_history_memory_correction_immutable();
"""
