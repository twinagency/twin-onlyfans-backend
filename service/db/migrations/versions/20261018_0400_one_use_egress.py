"""One-use wire marker without activation, replay or evidence reset."""
from pathlib import Path

from alembic import op

revision = "0073_one_use_egress"
down_revision = "0072_onlyfans_text_delivery"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        raise RuntimeError("OnlyFans one-use egress requires PostgreSQL")
    source = Path(__file__).resolve().parents[2] / "snapshots/0073_egress.sql"
    op.get_bind().exec_driver_sql(source.read_text())


def downgrade():
    raise RuntimeError("Forward-only wire evidence migration; pause and reconcile before code rollback")
