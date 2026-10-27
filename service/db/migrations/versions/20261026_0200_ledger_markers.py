"""Durable verified financial continuation and versioned coverage proof."""
from pathlib import Path
from alembic import op

revision = "0093_ledger_markers"
down_revision = "0092_ledger_history"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql((Path(__file__).resolve().parents[2] / "snapshots/0093_ledger_markers.sql").read_text())


def downgrade():
    raise RuntimeError("Retain receipt evidence and marker coverage; use a compatible forward fix")
