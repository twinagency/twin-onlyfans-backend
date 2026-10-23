"""FASTT runner: 'fastt' text-intent origin and the per-fan runtime owner marker."""
from pathlib import Path
from alembic import op

revision = "0092_fastt_runner"
down_revision = "0091_launch_controls"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql((Path(__file__).resolve().parents[2] / "snapshots/0092_fastt_runner.sql").read_text())


def downgrade():
    raise RuntimeError("Retain runner ownership and 'fastt' delivery evidence; use a compatible forward fix")
