"""Paid-offer import evidence, fixed catalogue pricing and continuous content sync."""
from pathlib import Path
from alembic import op

revision = "0089_offer_import"
down_revision = "0088_native_vault_media"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql((Path(__file__).resolve().parents[2] / "snapshots/0089_offer_import.sql").read_text())


def downgrade():
    raise RuntimeError("Retain offer evidence and import decisions; downgrade is not automatic")
