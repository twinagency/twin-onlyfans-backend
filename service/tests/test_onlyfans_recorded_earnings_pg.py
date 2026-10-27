from datetime import timedelta
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_onlyfans_earnings_pg import setup,read
from test_onlyfans_ledger_pg import record,parsed,transaction
from db.onlyfans_ledger_models import OfLedgerScan
from onlyfans_runtime.store import utcnow

from onlyfans_runtime.ledger import _coverage_receipt

def test_pending_earnings_are_included_without_counting_reversals(database):
    store=setup(database)
    for row in [transaction(id='cleared',amount='10.00',net='8.00'),transaction(id='pending',amount='3.00',net='2.40',status='loading'),transaction(id='refund',amount='50.00',status='undo')]:
        record(store,'acc-a',parsed(row))
    result=read(database,include_pending=True)
    assert result['status']=='current'
    assert result['clearedGrossCents']==1000
    r=result['recorded']
    assert (r['grossCents'],r['netCents'],r['pendingGrossCents'],r['pendingNetCents'],r['paymentCount'])==(1300,1040,300,240,2)
    assert 'recorded' not in read(database), 'existing callers keep their exact response contract'
    store.close()

def test_delayed_sync_keeps_recorded_payments_explicitly_unavailable_for_current_totals(database):
    store=setup(database)
    record(store,'acc-a',parsed(transaction(amount='3.00',net='2.40',status='loading')))
    with Session(database[1]) as s,s.begin():
        scan=s.get(OfLedgerScan,'acc-a');scan.status='held';scan.last_error='unsupported_ledger_response'
    result=read(database,include_pending=True)
    assert result['status']=='unavailable' and result['reasons']==['ledger_held']
    assert result['clearedGrossCents'] is None
    assert result['recorded']['grossCents']==300
    assert result['recorded']['lastSuccessfulSyncAt']==result['recentLedgerAt']
    with Session(database[1]) as s,s.begin():
        scan=s.get(OfLedgerScan,'acc-a');scan.last_full_at=utcnow()-timedelta(days=2);scan.coverage_json=_coverage_receipt(scan)
    assert read(database,include_pending=True)['recorded'] is None
    assert read(database,tenant='other',include_pending=True)['recorded'] is None
    store.close()
