"""Independent full-history progress; canonical transactions and evidence are reused."""
from pathlib import Path
from alembic import op
revision = "0092_ledger_history"
down_revision = "0091_launch_controls"
branch_labels = None
depends_on = None
def upgrade():
    op.get_bind().exec_driver_sql((Path(__file__).resolve().parents[2] / "snapshots/0092_ledger_history.sql").read_text())
def downgrade():
    raise RuntimeError("Preserve imported financial history; use a compatible forward fix")
