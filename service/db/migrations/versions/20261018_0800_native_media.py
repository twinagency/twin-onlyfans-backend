"""Carry existing media through the same one-use delivery authority."""
from pathlib import Path
from alembic import op

revision = "0077_native_media_sales"
down_revision = "0076_native_memory_evidence"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        raise RuntimeError("Native OnlyFans media requires PostgreSQL")
    op.get_bind().exec_driver_sql((Path(__file__).resolve().parents[2]/"snapshots/0077_native_media.sql").read_text())


def downgrade():
    raise RuntimeError("Forward-only delivery evidence; preserve media terms and receipts")
