"""New fan controls start off; existing operator decisions are preserved."""
from pathlib import Path
from alembic import op
revision = "0093_conversation_default_off"
down_revision = "0092_ledger_history"
branch_labels = None
depends_on = None

def upgrade():
    op.get_bind().exec_driver_sql((Path(__file__).resolve().parents[2] / "snapshots/0093_conversation_default_off.sql").read_text())

def downgrade():
    raise RuntimeError("Keep the safe off default; use a compatible forward fix")
