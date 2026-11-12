"""Allow the payment-evidence owner to hold a voluntary tip: of_fans_evidence.kind gains 'tip' (additive, data preserving).

Step 5.2. A provider-confirmed `tips.received` is recorded by the existing payment-evidence owner (`of_fans_evidence`) on the stable
provider tip identity, so a redelivery, a changed event key or a replay is one fact. The only schema change is the CHECK on
`kind`: it accepted 'unlock','transaction','chargeback','human_sent' and now also 'tip'. No column, index, trigger, row or
default changes; every existing row already satisfies the wider check, so nothing is rewritten and nothing is validated against
data that could fail.

Downgrade restores the narrower check and refuses while a 'tip' row exists: tip evidence (amounts, acknowledgement links) is
never destroyed to make a downgrade pass; roll the code back with the schema still at this revision instead.
"""
from alembic import op
from sqlalchemy import text

revision = "0138_tip_evidence_kind"
down_revision = "0137_model_retry_due_repair"
branch_labels = depends_on = None

CONSTRAINT = "ck_of_fans_evidence_kind"
OLD = "kind IN ('unlock','transaction','chargeback','human_sent')"
NEW = "kind IN ('unlock','transaction','chargeback','human_sent','tip')"


def upgrade():
    op.execute(f"ALTER TABLE of_fans_evidence DROP CONSTRAINT {CONSTRAINT}")
    op.execute(f"ALTER TABLE of_fans_evidence ADD CONSTRAINT {CONSTRAINT} CHECK({NEW})")


def downgrade():
    bind = op.get_bind()
    if bind.scalar(text("SELECT EXISTS (SELECT 1 FROM of_fans_evidence WHERE kind = 'tip')")):
        raise RuntimeError("Preserve tip evidence; use compatible code rollback at this schema revision")
    op.execute(f"ALTER TABLE of_fans_evidence DROP CONSTRAINT {CONSTRAINT}")
    op.execute(f"ALTER TABLE of_fans_evidence ADD CONSTRAINT {CONSTRAINT} CHECK({OLD})")
