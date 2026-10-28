"""Fan spend from reconciled ledger rows, with an explicit coverage boundary."""
from datetime import datetime,timedelta,timezone
from sqlalchemy import select,func
from db.models import Transaction
from db.onlyfans_models import OfRuntime
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_ledger_models import OfLedgerEvidence,OfLedgerHistory,OfLedgerScan
from onlyfans_runtime.ledger import coverage_proven
from .contract import iso

async def fan_spend(s,account,fan):
    runtime=await s.get(OfRuntime,account);connection=await s.get(OfProviderConnection,account)
    history=await s.get(OfLedgerHistory,account);scan=await s.get(OfLedgerScan,account)
    valid=bool(runtime and connection and history and scan and runtime.binding_id==connection.binding_id==history.binding_id==scan.binding_id
        and connection.owner_id==history.owner_id==scan.owner_id and connection.revision==history.connection_revision==scan.connection_revision)
    if not valid:return {'minorUnits':None,'currency':'USD','coverage':'unknown','asOf':None,'detail':'Financial import has not been verified for this connection.'}
    proof=select(OfLedgerEvidence.evidence_hash).where(OfLedgerEvidence.account_id==account,
        OfLedgerEvidence.fan_id==fan,OfLedgerEvidence.provider_transaction_id==Transaction.provider_transaction_id,OfLedgerEvidence.transaction_id==Transaction.id,OfLedgerEvidence.outcome=='applied').exists()
    total,count=(await s.execute(select(func.coalesce(func.sum(Transaction.amount_cents),0),func.count()).where(
        Transaction.account_id==account,Transaction.fan_id==fan,Transaction.status=='cleared',Transaction.currency=='USD',
        Transaction.source=='ledger',Transaction.amount_cents>=0,proof))).one()
    now=datetime.now(timezone.utc).replace(tzinfo=None)
    complete=bool(history.last_full_at and history.window_start==datetime(1970,1,1) and coverage_proven(history) and history.status!='held' and scan.status!='held' and scan.last_complete_at and timedelta(0)<=now-scan.last_complete_at<=timedelta(minutes=15) and timedelta(0)<=now-history.last_full_at<=timedelta(hours=26))
    return {'minorUnits':int(total) if count or complete else None,'currency':'USD','coverage':'complete' if complete else 'partial',
        'asOf':iso(scan.last_complete_at),'detail':'Reconciled cleared gross spend. Refunds and pending payments are excluded.' if complete else 'Recorded cleared spend only. Historical financial coverage is incomplete.'}
