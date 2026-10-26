"""Fence supported post-purchase replies at the existing offer receipt."""
from alembic import op

revision="0094_purchase_reply"
down_revision="0093_ledger_markers"
branch_labels=None
depends_on=None


def upgrade():
    op.get_bind().exec_driver_sql("ALTER TABLE content_offers ADD COLUMN post_buy_started_at TIMESTAMP WITHOUT TIME ZONE")


def downgrade():
    raise RuntimeError("Retain attempted purchase acknowledgements; use a compatible forward fix")
