"""Durable launch commands, source continuation and coordinated provider reads."""
from pathlib import Path
from alembic import op

revision = "0091_launch_controls"
down_revision = "0090_launch"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql((Path(__file__).resolve().parents[2] / "snapshots/0091_launch_controls.sql").read_text())


def downgrade():
    raise RuntimeError("Retain launch command, resolution and content occurrence evidence; use a compatible forward fix")
