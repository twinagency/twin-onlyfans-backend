"""Additive: one durable operator-case table for conversation holds (R1)."""
from alembic import op

revision = "0105_operator_cases"
down_revision = "0104_contextual_item_scope"
branch_labels = depends_on = None


def upgrade():
    from db.onlyfans_case_models import OfOperatorCase
    OfOperatorCase.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    # Open cases are safety evidence and hold conversations; never drop them on a rollback.
    pass
