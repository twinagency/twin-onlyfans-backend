"""Preserve bounded-page vault continuation past the first 4,800 items."""
from alembic import op
revision='0096_vault_continuation'
down_revision='0095_content_fold_pending'
branch_labels=None
depends_on=None

def upgrade():
    # Additive progress semantics: retain every job, frozen input and observation.
    # Network admission remains one 24-item page per account and worker pass.
    op.drop_constraint(op.f('ck_of_vault_sync_job_bounds'),'of_vault_sync_job',type_='check')
    op.create_check_constraint(op.f('ck_of_vault_sync_job_bounds'),'of_vault_sync_job',"connection_revision > 0 AND owner_id > 0 AND pages_read >= 0 AND max_pages = 200 AND items_seen >= 0 AND items_seen <= pages_read * 24 AND page_offset = items_seen")

def downgrade():
    raise RuntimeError('Do not truncate large-vault progress; use compatible code rollback')
