"""Twin bridge control tables, writer markers and the native FASTT pilot admission ledger."""
from pathlib import Path
from alembic import op

revision = "0092_native_pilot_storage"
down_revision = "0091_launch_controls"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        raise RuntimeError("Native pilot storage requires PostgreSQL")
    op.get_bind().exec_driver_sql((Path(__file__).resolve().parents[2] / "snapshots/0092_native_pilot_storage.sql").read_text())


def downgrade():
    raise RuntimeError("Retain pilot admission, receipt and writer-marker evidence; use a compatible forward fix")
