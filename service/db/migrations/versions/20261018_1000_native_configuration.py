"""Audit bounded native creator edits without resetting any financial policy."""
from pathlib import Path
from alembic import op

revision="0079_native_configuration"
down_revision="0078_operator_controls"
branch_labels=None
depends_on=None


def upgrade():
    if op.get_bind().dialect.name!="postgresql":raise RuntimeError("Native configuration requires PostgreSQL")
    op.get_bind().exec_driver_sql((Path(__file__).resolve().parents[2]/"snapshots/0079_native_configuration.sql").read_text())


def downgrade():
    raise RuntimeError("Forward-only configuration evidence; preserve all recorded changes")
