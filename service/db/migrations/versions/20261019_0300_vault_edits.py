"""Audit native edits to existing vault metadata and internal folders."""
from pathlib import Path
from alembic import op
revision="0086_vault_edits"
down_revision="0085_shared_creator_facts"
branch_labels=None
depends_on=None

def upgrade():
    if op.get_bind().dialect.name!="postgresql":raise RuntimeError("Vault edits require PostgreSQL")
    op.get_bind().exec_driver_sql((Path(__file__).resolve().parents[2]/"snapshots/0086_vault_edits.sql").read_text())

def downgrade():
    raise RuntimeError("Preserve operator edit evidence")
