"""Newest skipped fan row (tip/unsent/deleted) already counted as input: makes the input bump idempotent."""
from alembic import op
import sqlalchemy as sa
revision="0134_skipped_input"
down_revision="0133_preparation_retry_state"
branch_labels=depends_on=None
def present():  # init_db's additive catch-up may have added it already: up/down stay idempotent
    return "skipped_input_id" in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("of_conversation_state")}
def upgrade():
    if not present():
        op.add_column("of_conversation_state",sa.Column("skipped_input_id",sa.BigInteger(),nullable=True))
def downgrade():
    if present():
        op.drop_column("of_conversation_state","skipped_input_id")
