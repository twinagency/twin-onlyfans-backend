"""Record source-backed native corrections without a second memory store."""
from pathlib import Path
from alembic import op

revision = "0076_native_memory_evidence"
down_revision = "0075_native_model_budget"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        raise RuntimeError("Native OnlyFans memory requires PostgreSQL")
    op.get_bind().exec_driver_sql((Path(__file__).resolve().parents[2]/"snapshots/0076_native_memory.sql").read_text())


def downgrade():
    raise RuntimeError("Forward-only fact provenance; preserve corrections and evidence")
