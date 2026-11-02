"""Join history capacity and original-release migrations without rewriting either."""
revision = '0109_history_release_merge'
down_revision = ('0108_commercial_progress', '0106_parallel_history_reads')
branch_labels = depends_on = None

def upgrade():
    pass

def downgrade():
    pass
