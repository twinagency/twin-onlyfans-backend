"""Audited scoped metadata edits on the existing native catalogue."""
from pathlib import Path
from alembic import op

revision = "0082_content_management"
down_revision = "0081_native_connection"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        raise RuntimeError("OnlyFans content management requires PostgreSQL")
    op.get_bind().exec_driver_sql((Path(__file__).resolve().parents[2] / "snapshots/0082_content_management.sql").read_text())


def downgrade():
    raise RuntimeError("Forward-only content evidence; preserve all recorded changes")
