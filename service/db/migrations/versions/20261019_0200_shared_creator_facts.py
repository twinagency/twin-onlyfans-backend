"""Project approved Twin creator facts with explicit platform overrides."""
from pathlib import Path
from alembic import op

revision = "0085_shared_creator_facts"
down_revision = "0084_native_ledger"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        raise RuntimeError("Shared creator facts require PostgreSQL")
    op.get_bind().exec_driver_sql((Path(__file__).resolve().parents[2] / "snapshots/0085_shared_creator_facts.sql").read_text())


def downgrade():
    raise RuntimeError("Preserve creator source and override provenance")
