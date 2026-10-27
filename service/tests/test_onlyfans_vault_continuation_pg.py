"""Large vaults resume at their committed cursor, with original authority gates."""
from datetime import timedelta
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_onlyfans_connections_pg import verified, TENANT
from test_onlyfans_vault_pg import queue, media, page
from db.onlyfans_vault_models import OfVaultSyncJob, OfVaultObservation
from onlyfans_runtime.provider_connector import NativeProviderConnector
from onlyfans_runtime.vault_management import process_vault_jobs
from onlyfans_runtime.store import Store, utcnow


def test_vault_passes_4800_without_reset_or_false_completion(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    queued=queue(store,account)
    with Session(database[1]) as s,s.begin():
        job=s.get(OfVaultSyncJob,(account,queued['jobId']))
        job.pages_read=199; job.items_seen=4776; job.page_offset=4776
    offsets=[]
    def read(self,*,offset,limit):
        offsets.append(offset)
        assert limit==24 and store.engine.pool.checkedout()==0
        return page([media(offset+i+1) for i in range(24)],more=offset==4776)
    monkeypatch.setattr(NativeProviderConnector,'vault_page',read)
    assert process_vault_jobs(store,TENANT)==1
    with Session(database[1]) as s:
        job=s.get(OfVaultSyncJob,(account,queued['jobId']))
        assert job.status=='pending' and job.page_offset==4800 and job.pages_read==200
    store.close();store=Store(database[0])
    try:
        assert process_vault_jobs(store,TENANT)==1
        with Session(database[1]) as s:
            job=s.get(OfVaultSyncJob,(account,queued['jobId']))
            assert job.status=='complete' and job.page_offset==4824 and job.items_seen==4824
        assert offsets==[4776,4800]
    finally:store.close()

def test_vault_expiry_tracks_recent_progress_not_total_duration():
    from onlyfans_runtime.vault_management import _vault_job_expired
    now=utcnow()
    assert not _vault_job_expired(now-timedelta(hours=2),now-timedelta(minutes=5),now)
    assert _vault_job_expired(now-timedelta(hours=2),None,now)
    assert _vault_job_expired(now-timedelta(hours=2),now-timedelta(minutes=61),now)
