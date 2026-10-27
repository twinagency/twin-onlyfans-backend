"""Exact append-only DDL for Claude's unpublished migration integration.

These strings are inert. No startup, import or workflow operation executes DDL.
The owned tests install them only into an explicitly disposable local database.
"""
IMMUTABLE_EVIDENCE_SQL = (
    "CREATE FUNCTION of_personality_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ "
    "BEGIN RAISE EXCEPTION 'personality evidence is append-only'; END; $$",
    "CREATE TRIGGER of_personality_immutable BEFORE UPDATE OR DELETE ON of_personality_revision "
    "FOR EACH ROW EXECUTE FUNCTION of_personality_immutable()",
)
