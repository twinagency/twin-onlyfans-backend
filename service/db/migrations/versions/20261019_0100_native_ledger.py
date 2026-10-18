"""Read-only native transaction polling into the existing canonical ledger."""
from pathlib import Path
from alembic import op

revision = "0084_native_ledger"
down_revision = "0083_vault_management"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        raise RuntimeError("Native ledger requires PostgreSQL")
    op.get_bind().exec_driver_sql((Path(__file__).resolve().parents[2] / "snapshots/0084_native_ledger.sql").read_text())


def downgrade():
    raise RuntimeError("Forward-only ledger evidence; preserve recorded observations")
