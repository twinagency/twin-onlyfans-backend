"""A scoped maintenance turn cannot consume another account's queued job."""
from test_onlyfans_foundation_pg import database
from test_onlyfans_connections_pg import verified, TENANT
from test_onlyfans_vault_pg import queue
from sqlalchemy.orm import Session
from db.onlyfans_vault_models import OfVaultSyncJob
from onlyfans_runtime.vault_management import process_vault_jobs


def test_scoped_vault_turn_leaves_other_accounts_queued(database,monkeypatch):
    store,account,_=verified(database,monkeypatch)
    queue(store,account)
    try:
        assert process_vault_jobs(store,TENANT,account_id='other-account')==0
        with Session(store.engine) as s:
            row=s.query(OfVaultSyncJob).filter_by(account_id=account).one()
            assert row.status=='pending' and row.pages_read==0 and row.items_seen==0
    finally:store.close()
