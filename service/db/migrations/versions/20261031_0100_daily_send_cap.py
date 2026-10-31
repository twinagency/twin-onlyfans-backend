"""Optional per-account daily send cap beside the lifetime allowance; no data rewrite, default unchanged."""
from alembic import op
import sqlalchemy as sa

revision = "0101_daily_send_cap"
down_revision = "0100_manual_free_media"
branch_labels = depends_on = None


def upgrade():
    # NULL = no daily cap (today's behaviour). The lifetime limit_units column and its 0..10000 check are untouched.
    op.add_column("of_delivery_policy", sa.Column("daily_limit_units", sa.Integer(), nullable=True))
    op.create_check_constraint(op.f("ck_of_delivery_policy_daily_limit_units"), "of_delivery_policy",
                               "daily_limit_units IS NULL OR (daily_limit_units >= 0 AND daily_limit_units <= 20000)")


def downgrade():
    op.drop_constraint(op.f("ck_of_delivery_policy_daily_limit_units"), "of_delivery_policy", type_="check")
    op.drop_column("of_delivery_policy", "daily_limit_units")
