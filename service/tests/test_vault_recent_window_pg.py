import json
from datetime import timedelta
import pytest
from sqlalchemy import select,func,text
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_onlyfans_connections_pg import verified,TENANT,ACTOR
from test_onlyfans_vault_pg import media,page
from db.models import VaultItem
from db.onlyfans_vault_models import OfVaultSyncJob,OfVaultObservation
from onlyfans_runtime.provider_connector import NativeProviderConnector
from onlyfans_runtime.vault_management import request_sync,process_vault_jobs
from onlyfans_runtime.store import utcnow
import vault_import_policy as policy


def queue(store,account,n):
    return request_sync(store,TENANT,account,expected_connection_revision=1,command_key=f'{n:064x}',actor=ACTOR)


def provider(monkeypatch,store,data,calls):
    # Advance synthetic time between GETs so the REAL shared rate budget can
    # refill. No production budget, lease or retry guard is disabled.
    import onlyfans_runtime.vault_management as jobs
    import onlyfans_runtime.provider_read_budget as budget
    clock=[utcnow()]
    monkeypatch.setattr(jobs,'utcnow',lambda:clock[0])
    monkeypatch.setattr(budget,'utcnow',lambda:clock[0])
    def read(self,*,offset,limit=24,kind='all'):
        assert kind in ('photo','video') and store.engine.pool.checkedout()==0
        calls.append((kind,offset,limit))
        clock[0]+=timedelta(seconds=2)
        items=data[kind]
        return page(items[offset:offset+limit],offset+limit<len(items))
    monkeypatch.setattr(NativeProviderConnector,'vault_page',read)
    return clock


def drain(store,account,identity):
    for n in range(120):
        with Session(store.engine) as s:
            job=s.get(OfVaultSyncJob,(account,identity))
            if job.status=='complete':return job
            assert job.status in ('pending','running'),job.last_error
        assert process_vault_jobs(store,TENANT,limit=4)>0
    raise AssertionError('window did not complete within bounded pages')


def test_exact_cap_new_uploads_restart_and_no_history_deletion(database,monkeypatch):
    monkeypatch.setenv('ONLYFANS_VAULT_RECENT_WINDOW_ENABLED','1')
    store,account,_=verified(database,monkeypatch)
    data={'photo':[media(200000-n) for n in range(1501)],
          'video':[media(300000-n,'video') for n in range(701)]}
    calls=[];provider(monkeypatch,store,data,calls)
    first=queue(store,account,1);job=drain(store,account,first['jobId'])
    assert policy.load(job.window_json)['mode']=='window'
    assert all(offset+limit<=policy.CAPS[kind] for kind,offset,limit in calls)
    with Session(store.engine) as s:
        assert s.scalar(select(func.count()).select_from(VaultItem).where(VaultItem.kind=='photo'))==1500
        assert s.scalar(select(func.count()).select_from(VaultItem).where(VaultItem.kind=='video'))==700
        assert s.get(VaultItem,(account,198500)) is None
    # Simulate a new upload after reaching the cap. Existing scheduler requests
    # another job; it must scan the head, not rerun every old description.
    data['photo'].insert(0,media(400000,createdAt='2026-11-06T00:00:00Z'))
    data['video'].insert(0,media(500000,'video',createdAt='2026-11-06T00:00:00Z'))
    calls.clear();second=queue(store,account,2)
    # A restart between page applications retains typed cursor progress.
    process_vault_jobs(store,TENANT,limit=1)
    store.engine.dispose()
    completed=drain(store,account,second['jobId'])
    assert policy.load(completed.window_json)['mode']=='head'
    assert len(calls)==4
    with Session(store.engine) as s:
        assert s.get(VaultItem,(account,400000)) and s.get(VaultItem,(account,500000))
        assert s.get(VaultItem,(account,198501)).removed_at is None
        assert s.get(OfVaultObservation,(account,198501)) is not None
        active=list(s.scalars(select(VaultItem).where(VaultItem.account_id==account,policy.automatic_window())))
        assert sum(v.kind=='photo' for v in active)==1500 and sum(v.kind=='video' for v in active)==700
        assert 198501 not in {v.media_id for v in active}
    store.close()


def test_uncapped_active_job_is_fenced_before_provider_read(database,monkeypatch):
    store,account,_=verified(database,monkeypatch)
    old=queue(store,account,10)
    monkeypatch.setenv('ONLYFANS_VAULT_RECENT_WINDOW_ENABLED','1')
    calls=[];provider(monkeypatch,store,{'photo':[],'video':[]},calls)
    process_vault_jobs(store,TENANT,limit=1)
    with Session(store.engine) as s:
        job=s.get(OfVaultSyncJob,(account,old['jobId']))
        assert job.status=='superseded' and job.last_error=='vault_policy_changed'
    assert calls==[]
    new=queue(store,account,11);drain(store,account,new['jobId'])
    store.close()


def test_full_selected_window_reconciles_daily_and_scopes_coverage(database,monkeypatch):
    monkeypatch.setenv('ONLYFANS_VAULT_RECENT_WINDOW_ENABLED','1')
    store,account,_=verified(database,monkeypatch)
    calls=[];provider(monkeypatch,store,{'photo':[media(20)],'video':[]},calls)
    a=queue(store,account,20);drain(store,account,a['jobId'])
    with Session(store.engine) as s,s.begin():
        s.get(OfVaultSyncJob,(account,a['jobId'])).completed_at=utcnow()-timedelta(days=2)
    b=queue(store,account,21)
    with Session(store.engine) as s:
        assert policy.load(s.get(OfVaultSyncJob,(account,b['jobId'])).window_json)['mode']=='window'
    drain(store,account,b['jobId'])
    from onlyfans_runtime.content_sync import _vault_coverage
    with Session(store.engine) as s:
        s.execute(text('SET TRANSACTION READ ONLY'))
        view=_vault_coverage(s,account,True,utcnow())
        assert view['initialImport']['scope']['photos']==1500
        assert 'not a full-vault' in view['detail']
    # Completing a capped import must not conceal a later refresh failure.
    failed=queue(store,account,22)
    def refuse(self,**kwargs):raise RuntimeError('synthetic unavailable')
    monkeypatch.setattr(NativeProviderConnector,'vault_page',refuse)
    assert process_vault_jobs(store,TENANT,limit=1)==0
    with Session(store.engine) as s:
        view=_vault_coverage(s,account,True,utcnow())
        assert view['state']=='partial' and 'failed' in view['detail']
        assert 'not a full-vault' in view['detail']
    store.close()


def test_provider_backoff_preserves_typed_cursor_and_resumes(database,monkeypatch):
    from db.onlyfans_content_sync_progress_models import OfProviderReadBudget
    monkeypatch.setenv('ONLYFANS_VAULT_RECENT_WINDOW_ENABLED','1')
    store,account,_=verified(database,monkeypatch)
    calls=[]
    clock=provider(monkeypatch,store,{'photo':[media(51)],'video':[]},calls)
    queued=queue(store,account,30)
    with Session(store.engine) as s,s.begin():
        budget=s.get(OfProviderReadBudget,account)
        budget.backoff_until=clock[0]+timedelta(seconds=30)
    assert process_vault_jobs(store,TENANT,limit=1)==0 and calls==[]
    with Session(store.engine) as s:
        job=s.get(OfVaultSyncJob,(account,queued['jobId']))
        assert job.status=='running' and job.last_error.startswith('provider_read_deferred')
        assert policy.load(job.window_json)['photo']['offset']==0
        assert s.scalar(select(func.count()).select_from(VaultItem))==0
    clock[0]+=timedelta(seconds=31)
    drain(store,account,queued['jobId'])
    assert calls==[('photo',0,24),('video',0,24)]
    store.close()


def test_unsupported_type_filter_never_imports_wrong_type(database,monkeypatch):
    monkeypatch.setenv('ONLYFANS_VAULT_RECENT_WINDOW_ENABLED','1')
    store,account,_=verified(database,monkeypatch)
    calls=[];provider(monkeypatch,store,{'photo':[media(99,'video')],'video':[]},calls)
    queued=queue(store,account,40)
    assert process_vault_jobs(store,TENANT,limit=1)==0
    with Session(store.engine) as s:
        job=s.get(OfVaultSyncJob,(account,queued['jobId']))
        assert job.status=='failed' and job.last_error=='unsupported_vault_response'
        assert s.scalar(select(func.count()).select_from(VaultItem))==0
        assert policy.load(job.window_json)['photo']['offset']==0
    store.close()


def test_populated_additive_migration_preserves_existing_rows(database,monkeypatch):
    from alembic import command
    from db.schema import _config,check_schema,upgrade,MOXY
    from db.onlyfans_models import OfRuntime
    store,account,_=verified(database,monkeypatch)
    with Session(store.engine) as s,s.begin():
        s.add(VaultItem(account_id=account,media_id=811,kind='photo',created_at=utcnow(),
            description='Human correction',notes='Keep',locked_fields_json='["description"]'))
    with store.engine.begin() as c:
        command.downgrade(_config(c),MOXY)
        check_schema(c,expected=MOXY)
        assert c.execute(text('SELECT description,notes FROM vault_items WHERE media_id=811')).one()==('Human correction','Keep')
        upgrade(c)
        check_schema(c)
    with Session(store.engine) as s:
        item=s.get(VaultItem,(account,811))
        assert item.description=='Human correction' and item.locked_fields_json=='["description"]'
        assert s.get(OfRuntime,account).enabled is False
    monkeypatch.setenv('ONLYFANS_VAULT_RECENT_WINDOW_ENABLED','1')
    queued=queue(store,account,50)
    with pytest.raises(RuntimeError,match='Preserve recent-window progress'):
        with store.engine.begin() as c:command.downgrade(_config(c),MOXY)
    with store.engine.connect() as c:check_schema(c)
    with Session(store.engine) as s:
        assert s.get(OfVaultSyncJob,(account,queued['jobId'])).window_json
    store.close()


def test_approved_older_product_outside_window_keeps_existing_sale_path(database,monkeypatch):
    from test_moxy_integrated_pg import setup
    from test_moxy_media_descriptions_pg import offer
    from test_onlyfans_text_delivery_pg import ACCOUNT as account
    from onlyfans_runtime.text_delivery import SyntheticTextTransport
    from onlyfans_runtime.vault_description_worker import pending
    monkeypatch.setenv('ONLYFANS_VAULT_RECENT_WINDOW_ENABLED','1')
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    try:
        with Session(engine) as s,s.begin():
            s.add_all(VaultItem(account_id=account,media_id=900000+n,kind='photo',
                created_at=utcnow()+timedelta(days=1)) for n in range(1500))
        with Session(engine) as s:
            assert s.scalar(select(VaultItem.media_id).where(VaultItem.account_id==account,
                VaultItem.media_id==7101,policy.automatic_window())) is None
            assert s.scalar(select(VaultItem.media_id).where(VaultItem.account_id==account,
                VaultItem.media_id==7101,pending(utcnow()))) is None
        identity,_=offer(service,engine,'approved-old-outside-window')
        intent=service.queue(TENANT,account,identity)
        transport=SyntheticTextTransport(account)
        assert authority.dispatch(TENANT,account,intent,transport)=='accepted'
        assert len(transport.calls)==1 and transport.calls[0].media_ids==(7101,)
        assert transport.calls[0].price_cents==settings['products'][0]['price_cents']
    finally:store.close()


def test_main_and_dedicated_selectors_share_cap_and_prioritise_recent_media(database,monkeypatch):
    import importlib.util
    from pathlib import Path
    from db.models import Account
    from onlyfans_runtime.vault_description_worker import pending
    from native_pilot.tools.describe_imported_vault import select_batch
    monkeypatch.setenv('ONLYFANS_VAULT_RECENT_WINDOW_ENABLED','1')
    store,account,_=verified(database,monkeypatch)
    now=utcnow()
    with Session(store.engine) as s,s.begin():
        s.add(Account(id='other-creator'));s.flush()
        s.add_all(VaultItem(account_id=account,media_id=10000+n,kind='photo',
            created_at=now+timedelta(seconds=n)) for n in range(1501))
        s.add_all(VaultItem(account_id=account,media_id=20000+n,kind='video',
            created_at=now+timedelta(seconds=n)) for n in range(701))
        s.add(VaultItem(account_id='other-creator',media_id=999999,kind='photo',
            created_at=now+timedelta(days=2)))
    root=Path(__file__).resolve().parents[2]
    path=root/'compat/candidate/generic/onlyfans_runtime/vault_description_worker.py'
    spec=importlib.util.spec_from_file_location('onlyfans_runtime.generic_window_fixture',path)
    generic=importlib.util.module_from_spec(spec);spec.loader.exec_module(generic)
    with Session(store.engine) as s:
        for predicate in (pending,generic.pending):
            ids=set(s.scalars(select(VaultItem.media_id).where(
                VaultItem.account_id==account,predicate(now))))
            assert len(ids)==2200 and 10000 not in ids and 20000 not in ids
            assert {11500,20700}<=ids
        assert select_batch(s,account,4)==[11500,11499,11498,20700]
        # Rolling out of automatic work is not a catalogue deletion.
        assert s.get(VaultItem,(account,10000)).removed_at is None
    store.close()


def test_scheduled_window_polls_every_two_hours_without_delaying_first_import(database,monkeypatch):
    from types import SimpleNamespace
    from db.onlyfans_models import OfRuntime
    from onlyfans_runtime import content_sync
    import test_onlyfans_connections_pg as connections
    from datetime import datetime,timezone
    monkeypatch.setenv('ONLYFANS_VAULT_RECENT_WINDOW_ENABLED','1')
    monkeypatch.setattr(connections,'expires',lambda:datetime.now(timezone.utc)+timedelta(days=1))
    store,account,_=verified(database,monkeypatch)
    calls=[];clock=provider(monkeypatch,store,{'photo':[],'video':[]},calls)
    monkeypatch.setattr(content_sync,'utcnow',lambda:clock[0])
    with Session(store.engine) as s:binding=s.get(OfRuntime,account).binding_id
    def tick(identity):
        return content_sync._queue_vault(store,TENANT,SimpleNamespace(account=account,
            binding_id=binding,connection_revision=1,operation_id=identity,now=clock[0]),ACTOR)
    assert tick('initial')=='queued' and calls==[]
    with Session(store.engine) as s:
        first=s.scalar(select(OfVaultSyncJob).where(OfVaultSyncJob.account_id==account))
        identity=first.job_id
    initial=drain(store,account,identity)
    assert policy.load(initial.window_json)['mode']=='window'
    completed=initial.completed_at
    for seconds in (300,7199):
        clock[0]=completed+timedelta(seconds=seconds)
        assert tick('too-soon-'+str(seconds))=='recent'
    with Session(store.engine) as s:
        assert s.scalar(select(func.count()).select_from(OfVaultSyncJob))==1
    clock[0]=completed+timedelta(hours=2)
    assert tick('two-hours')=='queued'
    with Session(store.engine) as s:
        fresh=s.scalar(select(OfVaultSyncJob).where(OfVaultSyncJob.status=='pending'))
        identity=fresh.job_id
        assert policy.load(fresh.window_json)['mode']=='head'
    drain(store,account,identity)
    assert content_sync.INCREMENTAL_SECONDS==300  # Other source owners retain their schedule.
    store.close()


def test_selected_window_coverage_allows_two_hour_schedule(database,monkeypatch):
    from onlyfans_runtime.content_sync import _vault_coverage,_coverage_state
    monkeypatch.setenv('ONLYFANS_VAULT_RECENT_WINDOW_ENABLED','1')
    store,account,_=verified(database,monkeypatch)
    calls=[];provider(monkeypatch,store,{'photo':[],'video':[]},calls)
    queued=queue(store,account,70);complete=drain(store,account,queued['jobId'])
    with Session(store.engine) as s:
        for minutes in (30,121):
            view=_vault_coverage(s,account,True,complete.completed_at+timedelta(minutes=minutes))
            assert view['state']=='current' and 'overdue' not in view['detail']
        late=_vault_coverage(s,account,True,complete.completed_at+timedelta(hours=4,seconds=1))
        assert late['state']=='delayed' and 'two-hour schedule' in late['detail']
    assert _coverage_state(complete.completed_at+timedelta(seconds=601),complete.completed_at)=='delayed'
    store.close()


def test_recent_legacy_scan_does_not_delay_first_selected_import(database,monkeypatch):
    from types import SimpleNamespace
    from db.onlyfans_models import OfRuntime
    from onlyfans_runtime.content_sync import _queue_vault
    store,account,_=verified(database,monkeypatch)
    legacy=queue(store,account,80)
    monkeypatch.setenv('ONLYFANS_VAULT_RECENT_WINDOW_ENABLED','1')
    calls=[];provider(monkeypatch,store,{'photo':[],'video':[]},calls)
    process_vault_jobs(store,TENANT,limit=1)
    with Session(store.engine) as s:binding=s.get(OfRuntime,account).binding_id
    claim=SimpleNamespace(account=account,binding_id=binding,connection_revision=1,
        operation_id='first-selected-import',now=utcnow())
    assert _queue_vault(store,TENANT,claim,ACTOR)=='queued'
    assert calls==[]
    store.close()
