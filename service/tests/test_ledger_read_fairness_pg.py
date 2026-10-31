"""Real shared leases and ledger projection under deterministic message pressure."""
from datetime import timedelta
import pytest
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_onlyfans_launch_provider_budget_pg import native
from test_onlyfans_connections_pg import TENANT
from test_onlyfans_ledger_pg import payload, transaction
from db.onlyfans_ledger_models import OfLedgerScan
from db.onlyfans_content_sync_progress_models import OfProviderReadBudget
from onlyfans_runtime import ledger, provider_read_budget as budget
from onlyfans_runtime.provider_connector import NativeProviderConnector
from onlyfans_runtime.store import utcnow


def _scan_once(native, monkeypatch, now, *, more):
    store, account, _ = native
    monkeypatch.setattr(ledger, 'utcnow', lambda: now)
    monkeypatch.setattr(budget, 'utcnow', lambda: now)
    def page(self, **kw):
        return payload([], more=more)
    monkeypatch.setattr(NativeProviderConnector, 'transaction_page', page)
    assert ledger.process_ledger_pages(store, TENANT) == 1
    return store, account


def test_due_transactions_progress_between_repeated_message_reads(native, monkeypatch):
    store, account, _ = native
    clock = [utcnow()]
    monkeypatch.setattr(ledger, 'utcnow', lambda: clock[0])
    monkeypatch.setattr(budget, 'utcnow', lambda: clock[0])
    pages=[]
    def page(self, **kw):
        # Even a preferred reader cannot overlap its live lease.
        with pytest.raises(budget.ReadDeferred, match='lease occupied'):
            budget.acquire(store,TENANT,account,connection_revision=1,kind='recent')
        pages.append(kw['offset'])
        return payload([transaction(id='fair-'+str(len(pages)),description='Tip from Synthetic Fan')],more=len(pages)<4)
    monkeypatch.setattr(NativeProviderConnector,'transaction_page',page)
    for index in range(4):
        recent=budget.acquire(store,TENANT,account,connection_revision=1,kind='recent')
        assert recent.until == clock[0]+timedelta(seconds=45)
        assert ledger.process_ledger_pages(store,TENANT)==0
        with Session(store.engine) as s:
            scan=s.get(OfLedgerScan,account)
            assert scan.next_poll_at <= clock[0]+timedelta(seconds=2)
            assert scan.page_offset==index
            assert s.get(OfProviderReadBudget,account).tokens >= 2
        assert budget.release(store,TENANT,recent)
        clock[0]+=timedelta(seconds=2)
        with pytest.raises(budget.ReadDeferred,match='ledger turn due'):
            budget.acquire(store,TENANT,account,connection_revision=1,kind='recent')
        assert ledger.process_ledger_pages(store,TENANT)==1
        # Transaction progress yields back to replies, including while another
        # transaction page is due. No counter, cap or purchase state is reset.
        recent=budget.acquire(store,TENANT,account,connection_revision=1,kind='recent')
        assert budget.release(store,TENANT,recent)
        clock[0]+=timedelta(seconds=2)
    assert pages==[0,1,2,3]
    with Session(store.engine) as s:
        scan=s.get(OfLedgerScan,account)
        assert scan.status=='idle' and ledger.coverage_proven(scan)
    # Real provider backoff still takes precedence over the quicker retry.
    claim=budget.acquire(store,TENANT,account,connection_revision=1,kind='ledger')
    budget.release(store,TENANT,claim,retry_after=120)
    with pytest.raises(budget.ReadDeferred,match='backoff') as exc:
        budget.acquire(store,TENANT,account,connection_revision=1,kind='recent')
    assert exc.value.retry_at==clock[0]+timedelta(seconds=120)


def test_dead_ledger_worker_cannot_hold_replies_forever(native,monkeypatch):
    now=utcnow()
    store,account=_scan_once(native,monkeypatch,now,more=False)
    with Session(store.engine) as s,s.begin():
        row=s.get(OfProviderReadBudget,account)
        row.last_kind='recent'
        scan=s.get(OfLedgerScan,account)
        scan.next_poll_at=now
        scan.updated_at=now
    with pytest.raises(budget.ReadDeferred,match='ledger turn due'):
        budget.acquire(store,TENANT,account,connection_revision=1,kind='recent')
    later=now+timedelta(seconds=46)
    reply=budget.acquire(store,TENANT,account,connection_revision=1,kind='recent',now=later)
    assert budget.release(store,TENANT,reply,now=later)


def test_waiting_ledger_keeps_priority_even_after_a_ledger_page(native,monkeypatch):
    now=utcnow()
    store,account=_scan_once(native,monkeypatch,now,more=True)
    with Session(store.engine) as s,s.begin():
        row=s.get(OfProviderReadBudget,account)
        scan=s.get(OfLedgerScan,account)
        scan.last_error='provider_read_deferred:lease-occupied'
        scan.next_poll_at=now
        scan.updated_at=now
    with pytest.raises(budget.ReadDeferred,match='ledger turn due'):
        budget.acquire(store,TENANT,account,connection_revision=1,kind='recent',now=now)
    claim=budget.acquire(store,TENANT,account,connection_revision=1,kind='ledger',now=now)
    assert budget.release(store,TENANT,claim,now=now)


def test_due_running_ledger_scan_keeps_priority_before_next_page(native,monkeypatch):
    now=utcnow()
    store,account=_scan_once(native,monkeypatch,now,more=True)
    with Session(store.engine) as s,s.begin():
        row=s.get(OfProviderReadBudget,account)
        row.last_kind='recent'
        scan=s.get(OfLedgerScan,account)
        scan.last_error=None
        scan.next_poll_at=now
        scan.updated_at=now
    with pytest.raises(budget.ReadDeferred,match='ledger turn due'):
        budget.acquire(store,TENANT,account,connection_revision=1,kind='recent',now=now)
    with Session(store.engine) as s:
        scan=s.get(OfLedgerScan,account)
        assert scan.last_error==budget.LEDGER_TURN_RESERVED
    claim=budget.acquire(store,TENANT,account,connection_revision=1,kind='ledger',now=now)
    assert budget.release(store,TENANT,claim,now=now)
    later=now+timedelta(seconds=46)
    reply=budget.acquire(store,TENANT,account,connection_revision=1,kind='recent',now=later)
    assert budget.release(store,TENANT,reply,now=later)


def test_due_refresh_reserves_ledger_turn_but_expires_if_worker_is_dead(native,monkeypatch):
    now=utcnow()
    store,account=_scan_once(native,monkeypatch,now,more=False)
    with Session(store.engine) as s,s.begin():
        row=s.get(OfProviderReadBudget,account)
        row.last_kind='recent'
        scan=s.get(OfLedgerScan,account)
        scan.last_error=None
        scan.next_poll_at=now-timedelta(seconds=1)
        scan.updated_at=now-timedelta(seconds=300)
    with pytest.raises(budget.ReadDeferred,match='ledger turn due'):
        budget.acquire(store,TENANT,account,connection_revision=1,kind='recent',now=now)
    with Session(store.engine) as s:
        scan=s.get(OfLedgerScan,account)
        assert scan.last_error==budget.LEDGER_TURN_RESERVED and scan.updated_at==now
    later=now+timedelta(seconds=46)
    claim=budget.acquire(store,TENANT,account,connection_revision=1,kind='recent',now=later)
    assert budget.release(store,TENANT,claim,now=later)


def test_stale_partial_purchase_window_restarts_before_another_page(native,monkeypatch):
    now=utcnow()
    store,account=_scan_once(native,monkeypatch,now,more=True)
    with Session(store.engine) as s,s.begin():
        scan=s.get(OfLedgerScan,account)
        scan.last_error='provider_read_deferred:lease-occupied'
        scan.window_start=now-timedelta(days=7)
        scan.window_end=now-ledger.RECENT_COVERAGE_FRESHNESS
        scan.last_full_at=now-timedelta(hours=1)
        scan.page_offset=20
        scan.next_poll_at=now
        scan.updated_at=now
    seen=[]
    def page(self,**kw):
        seen.append((kw['offset'],kw['start'],kw['end']))
        return payload([],more=False)
    monkeypatch.setattr(NativeProviderConnector,'transaction_page',page)
    assert ledger.process_ledger_pages(store,TENANT)==1
    assert seen==[(0,now-timedelta(days=7),now)]
    with Session(store.engine) as s:
        scan=s.get(OfLedgerScan,account)
        assert scan.status=='idle' and scan.last_complete_at==now and scan.window_end==now
        assert ledger.coverage_proven(scan)
