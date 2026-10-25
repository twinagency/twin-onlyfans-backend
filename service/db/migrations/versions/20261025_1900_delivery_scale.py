"""Allow explicit production delivery limits without changing live settings."""
from pathlib import Path
from alembic import op
revision="0092_delivery_scale"
down_revision="0091_launch_controls"
branch_labels=None
depends_on=None
def upgrade():
    op.get_bind().exec_driver_sql((Path(__file__).resolve().parents[2]/"snapshots/0092_delivery_scale.sql").read_text())
def downgrade():
    raise RuntimeError("Retain delivery usage and coordinated schema compatibility; use a forward fix")
