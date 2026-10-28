"""Reconstruct historical shapes in disposable upgrade fixtures, never live DBs."""
from sqlalchemy import text


def restore_pre95_content_vault_shape(connection):
    assert connection.scalar(text('select current_database()')).startswith('of_test_')
    connection.exec_driver_sql('DROP TABLE of_ledger_exclusion')
    # These objects did not exist in revisions 0091–0094. Leaving them in a
    # fixture and changing only alembic_version is not a historical database.
    connection.exec_driver_sql('DROP TRIGGER of_content_fold_pending ON messages')
    connection.exec_driver_sql('DROP FUNCTION public.of_content_fold_pending()')
    connection.exec_driver_sql('ALTER TABLE of_content_sync_source DROP COLUMN fold_pending CASCADE')
    connection.exec_driver_sql('ALTER TABLE of_vault_sync_job DROP CONSTRAINT ck_of_vault_sync_job_bounds')
    connection.exec_driver_sql('ALTER TABLE of_vault_sync_job ADD CONSTRAINT ck_of_vault_sync_job_bounds CHECK (connection_revision > 0 AND owner_id > 0 AND pages_read >= 0 AND pages_read <= max_pages AND max_pages = 200 AND items_seen >= 0 AND items_seen <= 4800 AND page_offset = items_seen)')
    assert not connection.scalar(text("SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='of_content_sync_source' AND column_name='fold_pending'"))
