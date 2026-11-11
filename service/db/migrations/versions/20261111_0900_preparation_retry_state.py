"""Durable provider retry timing on the vault job and a sanitized last attempt on the preparation scope.

Both additions are nullable/defaulted and sit outside every column the scope guard trigger
compares (status, active_since, config, fingerprint, counters, identity), so the guard is
unchanged and revision/reservation rules still apply to every other update.
"""
from alembic import op
import sqlalchemy as sa
revision="0133_preparation_retry_state"
down_revision="0132_account_preparation"
branch_labels=depends_on=None
def upgrade():
    op.add_column("of_vault_sync_job",sa.Column("retry_not_before",sa.DateTime(),nullable=True))
    op.add_column("of_vault_sync_job",sa.Column("retry_attempts",sa.Integer(),nullable=False,server_default="0"))
    op.create_check_constraint(op.f("ck_of_vault_sync_job_retry_attempts"),"of_vault_sync_job","retry_attempts >= 0")
    op.add_column("of_engine_pilot_scope",sa.Column("last_attempt_json",sa.Text(),nullable=True))
def downgrade():
    bind=op.get_bind()
    if (bind.execute(sa.text("SELECT 1 FROM of_vault_sync_job WHERE retry_attempts>0 OR retry_not_before IS NOT NULL LIMIT 1")).first()
            or bind.execute(sa.text("SELECT 1 FROM of_engine_pilot_scope WHERE last_attempt_json IS NOT NULL LIMIT 1")).first()):
        raise RuntimeError("Preserve retry and attempt evidence; use compatible code rollback")
    op.drop_column("of_engine_pilot_scope","last_attempt_json")
    op.drop_constraint(op.f("ck_of_vault_sync_job_retry_attempts"),"of_vault_sync_job")
    op.drop_column("of_vault_sync_job","retry_attempts")
    op.drop_column("of_vault_sync_job","retry_not_before")
