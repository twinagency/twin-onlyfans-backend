"""History memory, personality workflow and proactive engagement (all default off)."""
from pathlib import Path
from alembic import op

revision = "0090_launch"
down_revision = "0089_offer_import"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql((Path(__file__).resolve().parents[2] / "snapshots/0090_launch.sql").read_text())


def downgrade():
    raise RuntimeError("Retain launch evidence (memory, personality, engagement); downgrade is not automatic")
