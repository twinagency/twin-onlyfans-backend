"""Explicit FansAPI provenance in the existing vault job, never native proof."""
from alembic import op
import sqlalchemy as sa
revision="0130_commercial_vault_provenance"
down_revision="0129_reconnect_receipt"
branch_labels=depends_on=None

def upgrade():
    op.add_column("of_vault_sync_job",sa.Column("provider_kind",sa.String(16),nullable=False,server_default="native"))
    op.add_column("of_vault_sync_job",sa.Column("provider_account",sa.String(255),nullable=True))
    op.create_check_constraint(op.f("ck_of_vault_sync_job_provider"),"of_vault_sync_job",
        "(provider_kind='native' AND provider_account IS NULL) OR (provider_kind='fansapi' AND provider_account IS NOT NULL)")
    op.execute("""CREATE FUNCTION of_vault_sync_provider_frozen() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF ROW(NEW.provider_kind,NEW.provider_account) IS DISTINCT FROM ROW(OLD.provider_kind,OLD.provider_account)
        THEN RAISE EXCEPTION 'vault provider provenance is immutable'; END IF;
      RETURN NEW;
    END $$""")
    op.execute("""CREATE TRIGGER of_vault_sync_provider_frozen BEFORE UPDATE ON of_vault_sync_job
        FOR EACH ROW EXECUTE FUNCTION of_vault_sync_provider_frozen()""")

def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM of_vault_sync_job WHERE provider_kind='fansapi' LIMIT 1")).first():
        raise RuntimeError("Preserve provider observations; use compatible code rollback")
    op.execute("DROP TRIGGER of_vault_sync_provider_frozen ON of_vault_sync_job")
    op.execute("DROP FUNCTION of_vault_sync_provider_frozen()")
    op.drop_constraint(op.f("ck_of_vault_sync_job_provider"),"of_vault_sync_job")
    op.drop_column("of_vault_sync_job","provider_account")
    op.drop_column("of_vault_sync_job","provider_kind")
