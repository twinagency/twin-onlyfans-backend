"""A verified connection automatically queues both imports without activating AI."""
import threading

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from test_onlyfans_connections_pg import verified, TENANT, ACTOR, save, change
from test_onlyfans_readonly_inbox_pg import forbid_outgoing
from test_onlyfans_vault_pg import media, page
from db.models import VaultItem, Message, GrokCall
from db.onlyfans_models import OfRuntime, OfScan, OfWork
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_offer_import_models import OfContentSyncAccount, OfOfferImportJob
from db.onlyfans_vault_models import OfVaultSyncJob
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from onlyfans_runtime.automatic_imports import ensure_started
from onlyfans_runtime.connections import load_worker_cohort
from onlyfans_runtime.content_sync import set_settings
from onlyfans_runtime.provider_connector import NativeProviderConnector
from onlyfans_runtime.vault_management import process_vault_jobs
from onlyfans_runtime.store import Refused
from onlyfans_read.agency_scope import actor_allowed, current_scope, AgencyScope


def count(store, model):
    with Session(store.engine) as s:return s.scalar(select(func.count()).select_from(model))


def test_connection_refresh_starts_inventory_and_vault_without_any_ai_authority(database,monkeypatch):
    store,account,_=verified(database,monkeypatch)
    forbid_outgoing(monkeypatch)
    monkeypatch.setenv('ONLYFANS_AUTOMATIC_IMPORTS_ENABLED','1')
    accounts,_,_=load_worker_cohort(store,TENANT,include_read_only=True)
    assert len(accounts)==1 and accounts[0].model is None
    with Session(store.engine) as s:
        row=s.get(OfContentSyncAccount,account)
        assert row.enabled and row.actor.startswith('worker-import:')
        assert row.initial_job_id is not None
        assert not s.get(OfRuntime,account).enabled
        assert s.get(OfScan,account) is not None
        assert s.get(OfDeliveryPolicy,account) is None
    assert count(store,OfOfferImportJob)==count(store,OfVaultSyncJob)==1
    for table in (OfWork,OfTextIntent,GrokCall):assert count(store,table)==0
    store.close()


def test_queued_vault_import_runs_without_a_manual_sync_command(database,monkeypatch):
    store,account,_=verified(database,monkeypatch)
    ensure_started(store,TENANT,account,connection_revision=1)
    calls=[]
    def read(self,**kw):
        assert store.engine.pool.checkedout()==0
        calls.append(kw['offset'])
        return page([media(501),media(502,'video')])
    monkeypatch.setattr(NativeProviderConnector,'vault_page',read)
    assert process_vault_jobs(store,TENANT,limit=1,stop=threading.Event())==1
    with Session(store.engine) as s:
        assert s.get(VaultItem,(account,501)) is not None
        assert s.get(VaultItem,(account,502)) is not None
        assert s.scalar(select(OfVaultSyncJob)).status=='complete'
    assert calls==[0]
    store.close()


def test_refresh_restart_and_reconnect_do_not_duplicate_or_reset_imports(database,monkeypatch):
    store,account,payload=verified(database,monkeypatch)
    assert ensure_started(store,TENANT,account,connection_revision=1)
    assert not ensure_started(store,TENANT,account,connection_revision=1)
    with Session(store.engine) as s,s.begin():
        job=s.scalar(select(OfVaultSyncJob))
        job.page_offset=job.items_seen=240
        job.pages_read=10
    assert save(store,account,payload,1,'d'*64)==2
    load_worker_cohort(store,TENANT,include_read_only=True)
    assert not ensure_started(store,TENANT,account,connection_revision=2)
    with Session(store.engine) as s:
        job=s.scalar(select(OfVaultSyncJob))
        assert job.page_offset==240 and job.connection_revision==2
    def continued(self,**kw):
        assert kw['offset']==240
        return page([media(601)])
    monkeypatch.setattr(NativeProviderConnector,'vault_page',continued)
    assert process_vault_jobs(store,TENANT,limit=1,stop=threading.Event())==1
    with Session(store.engine) as s:assert s.scalar(select(OfVaultSyncJob)).page_offset==241
    assert count(store,OfOfferImportJob)==count(store,OfVaultSyncJob)==1
    store.close()


def test_explicit_sync_off_is_respected(database,monkeypatch):
    store,account,_=verified(database,monkeypatch)
    set_settings(store,TENANT,account,enabled=False,expected_revision=0,command_key='e'*64,actor=ACTOR)
    assert not ensure_started(store,TENANT,account,connection_revision=1)
    assert count(store,OfOfferImportJob)==count(store,OfVaultSyncJob)==0
    with Session(store.engine) as s:assert not s.get(OfContentSyncAccount,account).enabled
    store.close()


def test_verification_revision_can_reuse_its_original_audited_capture(database,monkeypatch):
    store,account,_=verified(database,monkeypatch)
    change(store,account,'verify',1,'f'*64)
    load_worker_cohort(store,TENANT,include_read_only=True)
    assert ensure_started(store,TENANT,account,connection_revision=2)
    assert count(store,OfOfferImportJob)==count(store,OfVaultSyncJob)==1
    store.close()


@pytest.mark.parametrize('case',['wrong_tenant','old_revision','pending'])
def test_invalid_scope_or_unverified_connection_cannot_start_imports(database,monkeypatch,case):
    store,account,_=verified(database,monkeypatch)
    if case=='pending':
        with Session(store.engine) as s,s.begin():s.get(OfProviderConnection,account).status='pending'
    with pytest.raises(Refused):ensure_started(store,'other-tenant' if case=='wrong_tenant' else TENANT,account,
                                             connection_revision=2 if case=='old_revision' else 1)
    assert count(store,OfOfferImportJob)==count(store,OfVaultSyncJob)==0
    store.close()


def test_connection_change_before_settings_commit_rejects_stale_bootstrap(database,monkeypatch):
    store,account,payload=verified(database,monkeypatch)
    import onlyfans_runtime.content_sync as sync
    original=sync.set_settings
    def reconnected(*args,**kwargs):
        save(store,account,payload,1,'d'*64)
        return original(*args,**kwargs)
    monkeypatch.setattr(sync,'set_settings',reconnected)
    with pytest.raises(Refused):ensure_started(store,TENANT,account,connection_revision=1)
    assert count(store,OfOfferImportJob)==count(store,OfVaultSyncJob)==0
    store.close()


def test_worker_task_identity_is_rejected_by_api_and_agency_scopes(monkeypatch):
    actor='worker-import:'+'a'*64
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE','api')
    assert not actor_allowed(actor)
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE','worker')
    assert actor_allowed(actor)
    token=current_scope.set(AgencyScope(('creator-a',),'agency-user:synthetic',True))
    try:assert not actor_allowed(actor)
    finally:current_scope.reset(token)


def test_automatic_import_scheduler_runs_read_jobs_without_uploads(monkeypatch):
    import onlyfans_worker as entry
    import onlyfans_runtime.content_sync as sync
    import onlyfans_runtime.offer_post_source as posts
    import onlyfans_runtime.content_sync_media as uploads
    calls=[]
    monkeypatch.setattr(sync,'process_due',lambda *a,**kw:(calls.append('sync') or 1))
    monkeypatch.setattr(posts,'process_posts',lambda *a,**kw:(calls.append('posts') or 1))
    def forbidden(*a,**kw):pytest.fail('automatic reading entered the upload worker')
    monkeypatch.setattr(uploads,'process_uploads',forbidden)
    assert entry._process_import_sync(object(),TENANT,limit=1,stop=threading.Event())==2
    assert calls==['sync','posts']
