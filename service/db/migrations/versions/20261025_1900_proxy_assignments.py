"""Reserve fixed proxies independently of AI activation."""
from pathlib import Path
from alembic import op
revision="0092_proxy_assignments"
down_revision="0091_launch_controls"
branch_labels=None
depends_on=None
def upgrade():
    op.get_bind().exec_driver_sql((Path(__file__).resolve().parents[2]/"snapshots/0092_proxy_assignments.sql").read_text())
def downgrade():
    raise RuntimeError("Retain fixed proxy assignments; releasing them requires an explicit operator action")
