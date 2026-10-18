"""Synthetic-only durable text intents and immutable policy-change evidence.

No account is activated and no existing delivery budget is reset. The frozen
forward migration preserves earlier foundation and canonical history rows.
"""
from pathlib import Path

from alembic import op

revision = "0072_onlyfans_text_delivery"
down_revision = "0071_onlyfans_foundation"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        raise RuntimeError("OnlyFans text delivery requires PostgreSQL")
    source = Path(__file__).resolve().parents[2] / "snapshots/0072_delivery.sql"
    op.get_bind().exec_driver_sql(source.read_text())


def downgrade():
    raise RuntimeError("Forward-only delivery evidence migration; pause and reconcile before code rollback")
