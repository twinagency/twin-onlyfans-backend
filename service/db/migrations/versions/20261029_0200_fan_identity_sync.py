"""Bounded, resumable fan display-profile checks; no activation or data rewrite."""
from alembic import op
import sqlalchemy as sa

revision = "0099_fan_identity_sync"
down_revision = "0098_ledger_exclusion"
branch_labels = depends_on = None


def upgrade():
    op.add_column("fans", sa.Column("of_profile_checked_at", sa.DateTime(), nullable=True))
    op.add_column("fans", sa.Column("of_profile_error", sa.String(40), nullable=True))
    op.create_index("ix_fans_of_profile_due", "fans", ["account_id", "of_profile_checked_at", "fan_id"])


def downgrade():
    op.drop_index("ix_fans_of_profile_due", table_name="fans")
    op.drop_column("fans", "of_profile_error")
    op.drop_column("fans", "of_profile_checked_at")
