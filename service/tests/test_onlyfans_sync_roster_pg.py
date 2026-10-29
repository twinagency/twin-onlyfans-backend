"""Full-fan progress and current-session fencing on disposable PostgreSQL."""
from datetime import timedelta
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from test_onlyfans_launch_provider_budget_pg import native
from test_onlyfans_connections_pg import TENANT
from test_onlyfans_launch_history_memory_recovery_pg import ready, ACCOUNT, FAN, history
from db.models import Fan, OfConversationState
from db.onlyfans_models import OfRecovery, OfRuntime, OfWork
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_content_sync_progress_models import OfProviderReadBudget
from onlyfans_runtime.store import Refused, Store, utcnow
from onlyfans_runtime.provider_read_budget import acquire, release, ReadDeferred
from onlyfans_runtime.provider_connector import NativeReadTransientError
from onlyfans_runtime import profile_sync
from onlyfans_runtime.launch_history_memory.recovery import HistoryRecovery, OfHistoryCursor, OfHistoryScan
from onlyfans_runtime.worker import ChatPage, MessagePage


def add_fans(store, account, count=45):
    with Session(store.engine) as s,s.begin():
        for fan in range(20000,20000+count):s.add(Fan(account_id=account,fan_id=fan))
    with Session(store.engine) as s:
        return set(s.scalars(select(Fan.fan_id).where(Fan.account_id==account)))


def test_all_fans_beyond_37_resume_after_restart_without_ai_or_sends(native,monkeypatch):
    store,account,reader=native
    expected=add_fans(store,account)
    calls=[]
    def profiles(ids):
        assert store.engine.pool.checkedout()==0
        assert len(ids)<=20
        calls.extend(ids)
        # Missing data for an early fan must not pin the later batches.
        return {fan:{'id':fan,'name':'Synthetic '+str(fan),'username':'fixture'+str(fan)}
                for fan in ids if fan!=min(expected)}
    monkeypatch.setattr(reader,'profiles',profiles)
    assert profile_sync.step(store,TENANT,account,reader,connection_revision=1)==20
    restarted=Store(store.engine.url.render_as_string(hide_password=False))
    try:
        for _ in range(3):profile_sync.step(restarted,TENANT,account,reader,connection_revision=1)
        assert set(calls)==expected and len(calls)==len(expected)
        with Session(store.engine) as s:
            assert s.scalar(select(func.count()).select_from(OfWork))==0
            assert not s.get(OfRuntime,account).enabled
            assert not s.get(OfProviderConnection,account).automate_new_conversations
            assert s.get(Fan,(account,min(expected))).of_profile_error=='profile_unavailable'
            assert s.get(Fan,(account,min(expected))).profile_last_synced_at is None
            assert s.get(Fan,(account,max(expected))).of_display_name=='Synthetic '+str(max(expected))
            before=s.get(OfProviderReadBudget,account).tokens
        assert profile_sync.step(restarted,TENANT,account,reader,connection_revision=1)==0
        with Session(store.engine) as s:assert s.get(OfProviderReadBudget,account).tokens==before
        progress=HistoryRecovery(restarted).progress(TENANT,account)['fanProfiles']
        assert progress['known']==len(expected) and progress['missingNames']==1 and progress['unavailable']==1
        # A new lower-ID fan is immediately eligible, with no cursor reset.
        with Session(store.engine) as s,s.begin():s.add(Fan(account_id=account,fan_id=7))
        assert profile_sync.step(restarted,TENANT,account,reader,connection_revision=1)==1
        assert calls[-1]==7
    finally:restarted.close()


def test_profile_sync_honours_live_read_reserve_and_occupied_lease(native,monkeypatch):
    store,account,reader=native;add_fans(store,account,1)
    monkeypatch.setattr(reader,'profiles',lambda ids:pytest.fail('must not read'))
    claim=acquire(store,TENANT,account,connection_revision=1,kind='recent')
    with pytest.raises(ReadDeferred):profile_sync.step(store,TENANT,account,reader,connection_revision=1)
    release(store,TENANT,claim)
    with Session(store.engine) as s,s.begin():
        row=s.get(OfProviderReadBudget,account);row.tokens=3;row.refilled_at=utcnow()
    with pytest.raises(ReadDeferred):profile_sync.step(store,TENANT,account,reader,connection_revision=1)
    with Session(store.engine) as s:assert s.get(Fan,(account,20000)).of_profile_checked_at is None


@pytest.mark.parametrize('change',['revision','lease','binding'])
def test_profile_result_fenced_after_concurrent_change(native,monkeypatch,change):
    store,account,reader=native;add_fans(store,account,1)
    def profiles(ids):
        with Session(store.engine) as s,s.begin():
            if change=='revision':s.get(OfProviderConnection,account).revision+=1
            elif change=='lease':s.get(OfProviderReadBudget,account).lease_until=utcnow()-timedelta(seconds=1)
            else:
                from db.models import OfCreatorBinding
                s.get(OfCreatorBinding,s.get(OfRuntime,account).binding_id).revoked_at=utcnow()
        return {fan:{'id':fan,'name':'Must not commit'} for fan in ids}
    monkeypatch.setattr(reader,'profiles',profiles)
    with pytest.raises(Refused):profile_sync.step(store,TENANT,account,reader,connection_revision=1)
    with Session(store.engine) as s:
        fan=s.get(Fan,(account,20000))
        assert fan.of_profile_checked_at is None and fan.of_display_name is None


def test_failed_profile_batch_retains_reason_and_later_fans_get_a_turn(native,monkeypatch):
    store,account,reader=native;expected=add_fans(store,account)
    failure=NativeReadTransientError(60);failure.read_failure={'failure':'provider_timeout'}
    monkeypatch.setattr(reader,'profiles',lambda ids:(_ for _ in ()).throw(failure))
    with pytest.raises(NativeReadTransientError):profile_sync.step(store,TENANT,account,reader,connection_revision=1)
    with Session(store.engine) as s,s.begin():
        row=s.get(OfProviderReadBudget,account)
        assert row.backoff_until>utcnow()+timedelta(seconds=50)
        row.backoff_until=None
        assert s.get(Fan,(account,min(expected))).of_profile_error=='provider_timeout'
        assert s.get(Fan,(account,min(expected))).profile_last_synced_at is None
    calls=[]
    monkeypatch.setattr(reader,'profiles',lambda ids:calls.extend(ids) or {fan:{'id':fan,'name':'Next'} for fan in ids})
    assert profile_sync.step(store,TENANT,account,reader,connection_revision=1)==20
    assert min(expected) not in calls


def test_archive_discovery_commits_names_with_its_cursor(ready):
    store,recovery,engine=ready
    recovery.request_account('tenant-a',ACCOUNT)
    claim=recovery.claim_scan('tenant-a',ACCOUNT)
    recovery.scan_page('tenant-a',claim,[202,303],next_cursor='2',
        profiles={202:{'id':202,'name':'Synthetic Name'},303:{'id':303,'username':'synthetic303'}})
    with Session(engine) as s:
        assert s.get(Fan,(ACCOUNT,202)).of_display_name=='Synthetic Name'
        assert s.get(Fan,(ACCOUNT,303)).of_username=='synthetic303'
        assert s.get(OfHistoryScan,ACCOUNT).cursor=='2'
    with pytest.raises(Refused):
        recovery.scan_page('tenant-a',claim,[202],next_cursor=None,
            profiles={202:{'id':202,'name':'Stale overwrite'}})
    with Session(engine) as s:assert s.get(Fan,(ACCOUNT,202)).of_display_name=='Synthetic Name'


def test_later_fans_join_finished_archive_discovery_without_reset(ready):
    store,recovery,engine=ready
    recovery.request_account('tenant-a',ACCOUNT)
    scan=recovery.claim_scan('tenant-a',ACCOUNT)
    recovery.scan_page('tenant-a',scan,[],next_cursor=None)
    claim=recovery.claim('tenant-a',ACCOUNT)
    recovery.page('tenant-a',claim,[history(9)],has_more=False)
    assert recovery.progress('tenant-a',ACCOUNT)['status']=='complete'
    with Session(engine) as s,s.begin():
        s.add(Fan(account_id=ACCOUNT,fan_id=2))
        s.add(OfRecovery(account_id=ACCOUNT,fan_id=303))
    assert recovery.progress('tenant-a',ACCOUNT)['conversationsUnscheduled']==2
    assert recovery.progress('tenant-a',ACCOUNT)['status']=='incomplete'
    recovery.request_account('tenant-a',ACCOUNT)
    with Session(engine) as s:
        old=s.get(OfHistoryCursor,(ACCOUNT,FAN))
        assert old.status=='complete' and old.before_id==9 and old.pages_completed==1
        assert s.get(OfHistoryCursor,(ACCOUNT,2)).status=='needed'
        assert s.get(OfHistoryCursor,(ACCOUNT,303)).status=='needed'
        assert s.get(OfHistoryScan,ACCOUNT).status=='complete'


def test_archive_error_is_specific_and_waits_for_provider_backoff(ready):
    _,recovery,engine=ready
    recovery.request_account('tenant-a',ACCOUNT)
    error=NativeReadTransientError(120);error.read_failure={'failure':'provider_timeout'}
    class Reader:
        account_id=ACCOUNT
        def chats(self,**kw):return ChatPage([],None)
        def messages(self,**kw):raise error
    with pytest.raises(NativeReadTransientError):recovery.step('tenant-a',ACCOUNT,Reader())
    with Session(engine) as s:
        row=s.get(OfHistoryCursor,(ACCOUNT,FAN))
        assert row.last_error=='provider_timeout' and row.retry_at>utcnow()+timedelta(seconds=110)
        assert row.before_id is None and row.pages_completed==0


def test_profile_lookup_cannot_change_another_creators_same_fan(native,monkeypatch):
    from test_onlyfans_foundation_pg import seed
    store,account,reader=native;add_fans(store,account,1)
    other=seed(store.engine.url.render_as_string(hide_password=False),store.engine,
        account='other-creator',tenant=TENANT,fan=20000)
    try:
        monkeypatch.setattr(reader,'profiles',lambda ids:{fan:{'id':fan,'name':'Selected creator fan'} for fan in ids})
        profile_sync.step(store,TENANT,account,reader,connection_revision=1)
        with Session(store.engine) as s:
            assert s.get(Fan,(account,20000)).of_display_name=='Selected creator fan'
            foreign=s.get(Fan,('other-creator',20000))
            assert foreign.of_display_name is None and foreign.of_profile_checked_at is None
    finally:other.close()


def test_identity_migration_preserves_fans_and_runtime_controls(native):
    from sqlalchemy import text
    from alembic import command
    from db.schema import _config, check_schema, upgrade, LEDGER_EXCLUSION
    store,account,_=native;add_fans(store,account,1)
    with store.engine.begin() as c:
        c.execute(text("UPDATE fans SET of_display_name='Retained name',of_username='retained_handle' WHERE account_id=:a"),{'a':account})
        before_fans=c.execute(text('SELECT account_id,fan_id,of_display_name,of_username FROM fans ORDER BY account_id,fan_id')).all()
        before_runtime=c.execute(text('SELECT account_id,enabled,pending_events,recovery_needed FROM of_runtime ORDER BY account_id')).all()
        command.downgrade(_config(c),LEDGER_EXCLUSION)
        check_schema(c,expected=LEDGER_EXCLUSION)
        upgrade(c)
        check_schema(c)
        assert c.execute(text('SELECT account_id,fan_id,of_display_name,of_username FROM fans ORDER BY account_id,fan_id')).all()==before_fans
        assert c.execute(text('SELECT account_id,enabled,pending_events,recovery_needed FROM of_runtime ORDER BY account_id')).all()==before_runtime
        assert c.scalar(text('SELECT count(*) FROM fans WHERE of_profile_checked_at IS NOT NULL OR of_profile_error IS NOT NULL'))==0
