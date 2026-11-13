"""FL2-03: 0134 up/down are idempotent, so a DB whose init_db already added the column still upgrades."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect

from test_onlyfans_foundation_pg import database


def test_0134_upgrade_and_downgrade_are_idempotent(database):
    spec = spec_from_file_location("skipped_input_migration", Path(__file__).resolve().parents[1]
                                   / "db/migrations/versions/20261113_0100_skipped_input.py")
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    with database[1].begin() as c, Operations.context(MigrationContext.configure(c)):
        for step, present in (("upgrade", True), ("downgrade", False), ("downgrade", False), ("upgrade", True)):
            getattr(migration, step)()
            assert ("skipped_input_id" in {col["name"] for col in inspect(c).get_columns("of_conversation_state")}) is present
