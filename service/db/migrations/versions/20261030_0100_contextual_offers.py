"""Durable contextual-origin decisions. Existing sale/receipt tables remain authoritative."""
from alembic import op
import sqlalchemy as sa

revision = "0101_contextual_offers"
down_revision = "0100_manual_free_media"
branch_labels = depends_on = None


def upgrade():
    # Retain origin/uncertainty evidence across a compatible rollback and upgrade.
    from db.onlyfans_contextual_models import OfContextualOffer
    OfContextualOffer.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    # Alembic returns the version to 0100; extra additive evidence remains intact.
    # Off + stopped/drained contextual decisions is a release precondition.
    pass
