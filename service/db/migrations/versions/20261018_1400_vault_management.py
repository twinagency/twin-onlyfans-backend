"""Bounded selected-account vault reads and native catalogue creation evidence."""
from pathlib import Path
from alembic import op

revision = "0083_vault_management"
down_revision = "0082_content_management"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        raise RuntimeError("OnlyFans vault management requires PostgreSQL")
    op.get_bind().exec_driver_sql((Path(__file__).resolve().parents[2] / "snapshots/0083_vault_management.sql").read_text())


def downgrade():
    raise RuntimeError("Forward-only vault evidence; preserve all recorded changes")
