"""R5/R6 additive: sales decision log, durable invitations, canonical quote columns on content_offers."""
from alembic import op
import sqlalchemy as sa

revision = "0107_active_selling_quote"
down_revision = "0106_read_lane_fairness"
branch_labels = depends_on = None

_QUOTE_COLUMNS = (("quote_json", sa.Text()), ("currency", sa.String(3)), ("obligation_id", sa.String(64)),
                  ("lifecycle", sa.String(24)), ("quote_version", sa.Integer()))


def upgrade():
    bind = op.get_bind()
    # Idempotent: a database built by create_all already has everything below.
    have = {c["name"] for c in sa.inspect(bind).get_columns("content_offers")}
    for name, type_ in _QUOTE_COLUMNS:
        if name not in have:
            op.add_column("content_offers", sa.Column(name, type_, nullable=True))
    if "uq_content_offers_obligation" not in {i["name"] for i in sa.inspect(bind).get_indexes("content_offers")}:
        op.create_index("uq_content_offers_obligation", "content_offers", ["account_id", "obligation_id"], unique=True,
                        postgresql_where=sa.text("obligation_id IS NOT NULL AND status IN ('open','unlocked')"))
    from db.onlyfans_sales_models import OfSalesDecision, OfSalesInvitation
    OfSalesDecision.__table__.create(bind, checkfirst=True)
    OfSalesInvitation.__table__.create(bind, checkfirst=True)


def downgrade():
    # Decisions, invitations and quotes are sale evidence: never dropped. Columns are inert when unused.
    pass
