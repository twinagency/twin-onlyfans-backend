"""Durable campaign audiences and audited controls; no provider activation."""
from pathlib import Path
from alembic import op

revision = "0080_onlyfans_campaigns"
down_revision = "0079_native_configuration"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        raise RuntimeError("OnlyFans campaigns require PostgreSQL")
    op.get_bind().exec_driver_sql((Path(__file__).resolve().parents[2] / "snapshots/0080_onlyfans_campaigns.sql").read_text())


def downgrade():
    raise RuntimeError("Campaign evidence is forward-only; preserve recorded audiences and attempts")
