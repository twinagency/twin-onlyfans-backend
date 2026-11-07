"""Add a bounded, application-verified identity response proof."""
from alembic import op
import sqlalchemy as sa
revision = '0119_identity_independence'
down_revision = '0118_engine_operator'
branch_labels = depends_on = None


def upgrade():
    op.execute('ALTER TABLE of_engine_independence_proof DROP CONSTRAINT ck_of_engine_independence_proof_kind')
    op.execute("ALTER TABLE of_engine_independence_proof ADD CONSTRAINT ck_of_engine_independence_proof_kind CHECK (kind IN ('arithmetic.v1', 'identity.v1'))")


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM of_engine_independence_proof WHERE kind = 'identity.v1' LIMIT 1")).first():
        raise RuntimeError('Preserve identity independence evidence; use a compatible rollback')
    op.execute('ALTER TABLE of_engine_independence_proof DROP CONSTRAINT ck_of_engine_independence_proof_kind')
    op.execute("ALTER TABLE of_engine_independence_proof ADD CONSTRAINT ck_of_engine_independence_proof_kind CHECK (kind IN ('arithmetic.v1'))")
