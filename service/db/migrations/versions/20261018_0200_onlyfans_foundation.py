"""Durable scoped OnlyFans admission; no provider or automation enablement.

Parent is the published OnlyFans head. The independent Telegram 0065–0067
work is not present on this branch and requires a reviewed integration later.
"""
from pathlib import Path
from alembic import op

revision = "0071_onlyfans_foundation"
down_revision = "0070_onlyfans_read_bindings"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        raise RuntimeError("OnlyFans foundation requires PostgreSQL")
    source = Path(__file__).resolve().parents[2] / "snapshots/0071_foundation.sql"
    op.get_bind().exec_driver_sql(source.read_text())


def downgrade():
    # Intake and control evidence must survive code rollback. No automatic
    # destructive downgrade is offered for a populated foundation.
    raise RuntimeError("Forward-only evidence migration; restore/reconcile rehearsal required")
