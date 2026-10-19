"""Verify creator sessions before creating canonical account bindings."""
from pathlib import Path
from alembic import op
revision="0087_creator_enrollment"
down_revision="0086_vault_edits"
branch_labels=None
depends_on=None
def upgrade():
    if op.get_bind().dialect.name!="postgresql":raise RuntimeError("Creator connection enrollment requires PostgreSQL")
    op.get_bind().exec_driver_sql((Path(__file__).resolve().parents[2]/"snapshots/0087_creator_enrollment.sql").read_text())
def downgrade():
    raise RuntimeError("Preserve connection enrollment evidence")
