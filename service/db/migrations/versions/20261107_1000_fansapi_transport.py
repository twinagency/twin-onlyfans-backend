"""Allow the real Fans API provider as an engine delivery kind (kept apart from 'synthetic' and 'native')."""
from alembic import op
import sqlalchemy as sa

revision = '0122_fansapi_transport'
down_revision = '0121_engine_generations'
branch_labels = depends_on = None


def upgrade():
    op.execute("ALTER TABLE of_delivery_policy DROP CONSTRAINT ck_of_delivery_policy_transport_kind")
    op.execute("ALTER TABLE of_delivery_policy ADD CONSTRAINT ck_of_delivery_policy_transport_kind "
               "CHECK (transport_kind IN ('synthetic','native','fansapi'))")


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM of_delivery_policy WHERE transport_kind = 'fansapi' LIMIT 1")).first():
        raise RuntimeError("Move every fansapi delivery policy back to synthetic before downgrading")
    op.execute("ALTER TABLE of_delivery_policy DROP CONSTRAINT ck_of_delivery_policy_transport_kind")
    op.execute("ALTER TABLE of_delivery_policy ADD CONSTRAINT ck_of_delivery_policy_transport_kind "
               "CHECK (transport_kind IN ('synthetic','native'))")
