"""Durable native upload admission and requested video preparation."""
from pathlib import Path
from alembic import op

revision = "0088_native_vault_media"
down_revision = "0087_creator_enrollment"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql((Path(__file__).resolve().parents[2] / "snapshots/0088_native_vault_media.sql").read_text())


def downgrade():
    raise RuntimeError("Retain upload and carrier-recovery evidence; downgrade is not automatic")
