"""Restart-safe native text preparation; no model/provider activation."""
from pathlib import Path
from alembic import op

revision = "0074_native_text_plans"
down_revision = "0073_one_use_egress"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        raise RuntimeError("Native OnlyFans plans require PostgreSQL")
    source = Path(__file__).resolve().parents[2] / "snapshots/0074_native_text.sql"
    op.get_bind().exec_driver_sql(source.read_text())


def downgrade():
    raise RuntimeError("Forward-only model/plan evidence; retain uncertainty before code rollback")
