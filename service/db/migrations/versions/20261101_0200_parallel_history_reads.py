"""Independent provider read leases under the shared account rate budget."""
from alembic import op
import sqlalchemy as sa

revision = '0105_parallel_history_reads'
down_revision = '0104_contextual_item_scope'
branch_labels = depends_on = None


def upgrade():
    op.add_column('of_provider_read_budget', sa.Column('rate_ceiling', sa.Integer()))
    op.add_column('of_provider_read_budget', sa.Column('rate_recover_at', sa.DateTime()))
    op.add_column('of_launch_history_cursor', sa.Column('page_limit', sa.Integer(), nullable=False, server_default='20'))
    op.create_check_constraint('ck_of_launch_history_cursor_page_limit', 'of_launch_history_cursor', 'page_limit >= 5 AND page_limit <= 100')
    op.drop_constraint('ck_of_provider_read_budget_tokens', 'of_provider_read_budget', type_='check')
    op.create_check_constraint('ck_of_provider_read_budget_tokens', 'of_provider_read_budget', 'tokens >= 0 AND tokens <= 64')
    op.create_table('of_provider_read_lease',
        sa.Column('token', sa.String(36), primary_key=True),
        sa.Column('account_id', sa.String(), sa.ForeignKey('of_runtime.account_id', ondelete='RESTRICT'), nullable=False),
        sa.Column('binding_id', sa.String(), sa.ForeignKey('of_creator_bindings.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('connection_revision', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(32), nullable=False),
        sa.Column('lease_until', sa.DateTime(), nullable=False))
    op.create_index('ix_of_provider_read_lease_account_until', 'of_provider_read_lease', ['account_id', 'lease_until'])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM of_provider_read_lease WHERE lease_until>CURRENT_TIMESTAMP LIMIT 1')).first():
        raise RuntimeError('Drain provider read workers before removing their leases')
    op.drop_index('ix_of_provider_read_lease_account_until', table_name='of_provider_read_lease')
    op.drop_table('of_provider_read_lease')
    op.drop_column('of_provider_read_budget', 'rate_ceiling')
    op.drop_column('of_provider_read_budget', 'rate_recover_at')
    op.drop_constraint('ck_of_launch_history_cursor_page_limit', 'of_launch_history_cursor', type_='check')
    op.drop_column('of_launch_history_cursor', 'page_limit')
    op.execute('UPDATE of_provider_read_budget SET tokens=LEAST(tokens,10)')
    op.drop_constraint('ck_of_provider_read_budget_tokens', 'of_provider_read_budget', type_='check')
    op.create_check_constraint('ck_of_provider_read_budget_tokens', 'of_provider_read_budget', 'tokens >= 0 AND tokens <= 10')
