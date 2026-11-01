"""Durable bounded progress on the existing conversation authority; inert until enabled."""
from alembic import op
import sqlalchemy as sa

revision = "0108_commercial_progress"
down_revision = "0107_active_selling_quote"
branch_labels = depends_on = None


def upgrade():
    bind = op.get_bind()
    if "commercial_state_json" not in {c["name"] for c in sa.inspect(bind).get_columns("of_conversation_state")}:
        op.add_column("of_conversation_state", sa.Column("commercial_state_json", sa.Text(), nullable=True))
    columns = {c["name"] for c in sa.inspect(bind).get_columns("of_sales_invitation")}
    for name, kind in (("offer_terms_json", sa.Text()), ("invitation_body", sa.Text()), ("delivery_message_id", sa.BigInteger())):
        if name not in columns:
            op.add_column("of_sales_invitation", sa.Column(name, kind, nullable=True))
    columns = {c["name"] for c in sa.inspect(bind).get_columns("of_text_intent")}
    for name, kind in (("quote_json", sa.Text()), ("quote_lifecycle", sa.String(24))):
        if name not in columns:
            op.add_column("of_text_intent", sa.Column(name, kind, nullable=True))


def downgrade():
    # Attempt evidence and fulfilment history must survive a disabled feature.
    pass
