"""Account-specific concurrent delivery; defaults preserve the existing gap mode."""
from alembic import op
import sqlalchemy as sa


revision = "0103_concurrent_delivery"
down_revision = "0102_daily_send_cap"
branch_labels = depends_on = None


def upgrade():
    op.add_column("of_delivery_policy", sa.Column("delivery_mode", sa.String(16), nullable=False,
                                                  server_default=sa.text("'gap'")))
    op.add_column("of_delivery_policy", sa.Column("provider_requests_per_second", sa.Float(), nullable=False,
                                                  server_default=sa.text("1.0")))
    op.add_column("of_delivery_policy", sa.Column("provider_burst", sa.Integer(), nullable=False,
                                                  server_default=sa.text("5")))
    op.add_column("of_delivery_policy", sa.Column("provider_max_concurrent", sa.Integer(), nullable=False,
                                                  server_default=sa.text("4")))
    op.add_column("of_delivery_policy", sa.Column("conversation_min_gap_seconds", sa.Integer(), nullable=False,
                                                  server_default=sa.text("0")))
    op.add_column("of_delivery_policy", sa.Column("provider_cooldown_until", sa.DateTime(), nullable=True))
    op.create_check_constraint(op.f("ck_of_delivery_policy_delivery_mode"), "of_delivery_policy",
                               "delivery_mode IN ('gap','concurrent')")
    op.create_check_constraint(op.f("ck_of_delivery_policy_provider_requests_per_second"), "of_delivery_policy",
                               "provider_requests_per_second > 0 AND provider_requests_per_second <= 100")
    op.create_check_constraint(op.f("ck_of_delivery_policy_provider_burst"), "of_delivery_policy",
                               "provider_burst >= 1 AND provider_burst <= 200")
    op.create_check_constraint(op.f("ck_of_delivery_policy_provider_max_concurrent"), "of_delivery_policy",
                               "provider_max_concurrent >= 1 AND provider_max_concurrent <= 64")
    op.create_check_constraint(op.f("ck_of_delivery_policy_conversation_min_gap_seconds"), "of_delivery_policy",
                               "conversation_min_gap_seconds >= 0 AND conversation_min_gap_seconds <= 3600")


def downgrade():
    for name in ("ck_of_delivery_policy_conversation_min_gap_seconds",
                 "ck_of_delivery_policy_provider_max_concurrent",
                 "ck_of_delivery_policy_provider_burst",
                 "ck_of_delivery_policy_provider_requests_per_second",
                 "ck_of_delivery_policy_delivery_mode"):
        op.drop_constraint(op.f(name), "of_delivery_policy", type_="check")
    for column in ("provider_cooldown_until", "conversation_min_gap_seconds", "provider_max_concurrent",
                   "provider_burst", "provider_requests_per_second", "delivery_mode"):
        op.drop_column("of_delivery_policy", column)
