"""Keep one uncertain delivery per fan; retain multiple confirmed unpaid cards."""
from alembic import op
import sqlalchemy as sa

revision = "0104_contextual_item_scope"
down_revision = "0103_concurrent_delivery"
branch_labels = depends_on = None


def upgrade():
    op.drop_index("uq_contextual_unresolved", table_name="of_contextual_offer")
    op.create_index("uq_contextual_unresolved", "of_contextual_offer", ["account_id", "fan_id"], unique=True,
                    postgresql_where=sa.text("state IN ('reserved','unknown')"),
                    sqlite_where=sa.text("state IN ('reserved','unknown')"))


def downgrade():
    # Restoring the old index can fail after legitimate different-item sales.
    # Refuse explicitly rather than delete or relabel financial evidence.
    conflict = op.get_bind().execute(sa.text("SELECT 1 FROM of_contextual_offer "
        "WHERE state IN ('reserved','sent','unknown') GROUP BY account_id,fan_id HAVING count(*) > 1 LIMIT 1")).first()
    if conflict:
        raise RuntimeError("Cannot restore fan-wide offer limit while multiple confirmed cards exist")
    op.drop_index("uq_contextual_unresolved", table_name="of_contextual_offer")
    op.create_index("uq_contextual_unresolved", "of_contextual_offer", ["account_id", "fan_id"], unique=True,
                    postgresql_where=sa.text("state IN ('reserved','sent','unknown')"),
                    sqlite_where=sa.text("state IN ('reserved','sent','unknown')"))
