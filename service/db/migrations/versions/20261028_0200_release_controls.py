"""Join the deployed default-off branch and CRM reliability migrations."""
revision = "0097_release_controls"
down_revision = ("0096_vault_continuation", "0093_conversation_default_off")
branch_labels = None
depends_on = None

def upgrade():
    pass

def downgrade():
    raise RuntimeError("Preserve both deployed branches; use a compatible forward fix")
