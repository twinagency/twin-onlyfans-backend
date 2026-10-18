"""Link native plans to the existing cost owner without reopening any allowance."""
from pathlib import Path
from alembic import op

revision = "0075_native_model_budget"
down_revision = "0074_native_text_plans"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        raise RuntimeError("Native OnlyFans model budgets require PostgreSQL")
    source = Path(__file__).resolve().parents[2] / "snapshots/0075_native_budget.sql"
    op.get_bind().exec_driver_sql(source.read_text())


def downgrade():
    raise RuntimeError("Forward-only cost evidence; retain reservations and unknowns")
