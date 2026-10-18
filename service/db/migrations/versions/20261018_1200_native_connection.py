"""Explicit encrypted account connection and native transport policy."""
from pathlib import Path
from alembic import op
revision="0081_native_connection"
down_revision="0080_onlyfans_campaigns"
branch_labels=None
depends_on=None
def upgrade():
    if op.get_bind().dialect.name!="postgresql":raise RuntimeError("OnlyFans connections require PostgreSQL")
    op.get_bind().exec_driver_sql((Path(__file__).resolve().parents[2]/"snapshots/0081_native_connection.sql").read_text())
def downgrade():
    raise RuntimeError("Preserve encrypted connection and lifecycle evidence")
