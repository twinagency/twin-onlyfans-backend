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
    store,account,_=native
    now=utcnow()
    monkeypatch.setattr(ledger,'utcnow',lambda:now)
    monkeypatch.setattr(budget,'utcnow',lambda:now)
    claim=budget.acquire(store,TENANT,account,connection_revision=1,kind='recent')
    assert ledger.process_ledger_pages(store,TENANT)==0
    budget.release(store,TENANT,claim)
    with pytest.raises(budget.ReadDeferred,match='ledger turn due'):
        budget.acquire(store,TENANT,account,connection_revision=1,kind='recent')
    later=now+timedelta(seconds=46)
    reply=budget.acquire(store,TENANT,account,connection_revision=1,kind='recent',now=later)
    assert budget.release(store,TENANT,reply,now=later)
