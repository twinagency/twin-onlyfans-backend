"""R4 additive: bounded-starvation marker on the read budget; stored substitution agreement."""
from alembic import op
import sqlalchemy as sa

revision = "0106_read_lane_fairness"
down_revision = "0105_operator_cases"
branch_labels = depends_on = None


def upgrade():
    # Idempotent: a database built by create_all already has both columns.
    have = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("of_provider_read_budget")}
    if "starved_kind" not in have:
        op.add_column("of_provider_read_budget", sa.Column("starved_kind", sa.String(32), nullable=True))
    if "starved_since" not in have:
        op.add_column("of_provider_read_budget", sa.Column("starved_since", sa.DateTime(), nullable=True))
    from db.onlyfans_substitution_models import OfSubstitutionAgreement
    OfSubstitutionAgreement.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    # Agreements are sale evidence: never dropped. The starvation columns are inert when unused.
    pass
