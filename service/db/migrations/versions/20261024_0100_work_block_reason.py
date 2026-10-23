"""Blocked-work reason/phase and routine-poll marker (additive)."""
from pathlib import Path
from alembic import op

revision = "0092_work_block_reason"
down_revision = "0091_launch_controls"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql((Path(__file__).resolve().parents[2] / "snapshots/0092_work_block_reason.sql").read_text())


def downgrade():
    raise RuntimeError("Retain blocked-work evidence; use a compatible forward fix")
