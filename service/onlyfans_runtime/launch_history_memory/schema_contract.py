"""SQL for Claude's next combined migration; importing never executes DDL.

The two independent registries intentionally do not change the shared schema
head. Tests may create their tables ONLY on a newly bootstrapped disposable DB.
The integration owner chooses the next revision after unpublished 0089.
"""
EVIDENCE_GUARDS_SQL = """
CREATE FUNCTION of_history_memory_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'historical memory evidence is append-only';
END;
$$;
CREATE TRIGGER history_memory_evidence_append_only
BEFORE UPDATE OR DELETE ON of_history_memory_evidence
FOR EACH ROW EXECUTE FUNCTION of_history_memory_append_only();
CREATE TRIGGER history_memory_correction_append_only
BEFORE UPDATE OR DELETE ON of_history_memory_correction
FOR EACH ROW EXECUTE FUNCTION of_history_memory_append_only();
"""


def migration_sql():
    """Return reviewable PostgreSQL DDL without running a migration."""
    from sqlalchemy.dialects.postgresql import dialect
    from sqlalchemy.schema import CreateIndex, CreateTable
    from .models import MemoryBase
    from .recovery import RecoveryBase
    from . import recent  # noqa: F401 - owned bootstrap metadata, no shared registration
    statements = []
    for metadata in (MemoryBase.metadata, RecoveryBase.metadata):
        for table in metadata.sorted_tables:
            statements.append(str(CreateTable(table).compile(dialect=dialect())) + ";")
            statements.extend(str(CreateIndex(index).compile(dialect=dialect())) + ";"
                              for index in sorted(table.indexes, key=lambda i: i.name))
    return "\n\n".join(statements) + "\n" + EVIDENCE_GUARDS_SQL
