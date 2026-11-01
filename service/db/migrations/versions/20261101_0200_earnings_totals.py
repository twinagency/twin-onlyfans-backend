"""Provider-reported earnings totals per account and period (reporting cache only)."""
from alembic import op
import sqlalchemy as sa

revision = "0105_earnings_totals"
down_revision = "0104_contextual_item_scope"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "of_earnings_total",
        sa.Column("account_id", sa.String(), sa.ForeignKey("of_runtime.account_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("period", sa.String(8), primary_key=True),
        sa.Column("binding_id", sa.String(), nullable=False),
        sa.Column("connection_revision", sa.Integer(), nullable=False),
        sa.Column("gross_cents", sa.BigInteger(), nullable=False),
        sa.Column("net_cents", sa.BigInteger(), nullable=False),
        sa.Column("window_start", sa.DateTime(), nullable=False),
        sa.Column("window_end", sa.DateTime(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("period IN ('7d','30d','90d','all')", name="ck_earnings_total_period"),
        sa.CheckConstraint("gross_cents >= 0 AND net_cents >= 0", name="ck_earnings_total_amounts"),
    )


def downgrade():
    # A pure cache of provider statistics: dropping it loses nothing that cannot be refetched.
    op.drop_table("of_earnings_total")
