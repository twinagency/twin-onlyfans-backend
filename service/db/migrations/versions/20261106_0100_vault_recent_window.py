"""Add progress to the existing vault job; preserve all history and owners."""
from alembic import op
import sqlalchemy as sa

revision = '0112_vault_recent_window'
down_revision = '0111_moxy_owner'
branch_labels = depends_on = None


def upgrade():
    op.add_column('of_vault_sync_job', sa.Column('window_json', sa.Text(), nullable=True))
    op.create_index('ix_vault_recent_window', 'vault_items', ['account_id','kind','created_at','media_id'])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM of_vault_sync_job WHERE window_json IS NOT NULL LIMIT 1')).first():
        raise RuntimeError('Preserve recent-window progress; use a compatible code rollback')
    op.drop_index('ix_vault_recent_window', table_name='vault_items')
    op.drop_column('of_vault_sync_job', 'window_json')
