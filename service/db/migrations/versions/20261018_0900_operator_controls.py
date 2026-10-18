"""Audit explicit operator controls without reopening delivery or model rights."""
from pathlib import Path
from alembic import op

revision = "0078_operator_controls"
down_revision = "0077_native_media_sales"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        raise RuntimeError("OnlyFans operator controls require PostgreSQL")
    op.get_bind().exec_driver_sql((Path(__file__).resolve().parents[2]/"snapshots/0078_operator_controls.sql").read_text())


def downgrade():
    raise RuntimeError("Forward-only control evidence; preserve revisions and holds")
