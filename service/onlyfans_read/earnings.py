"""Read-only, tenant-bound totals from canonical native ledger observations.

Amounts are not offers, opens or fan balances. A missing baseline is unknown,
never zero. Refund/pending states stay separate from cleared gross receipts.
"""
from datetime import timedelta
from sqlalchemy import select,func,and_,text
from db.models import Transaction,OfCreatorBinding
from db.onlyfans_models import OfRuntime
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_ledger_models import OfLedgerEvidence,OfLedgerScan
from onlyfans_runtime.text_delivery import _stamp
from onlyfans_runtime.store import Refused as InvalidStamp
from .errors import BadRequest,Unavailable

SCHEMA="of-earnings.v1"
REASONS={"no_bound_accounts","ledger_unavailable","ledger_stale","ledger_held","outside_coverage","unsupported_currency","unsupported_status","unverified_ledger_rows"}


def stamp(value):return value.isoformat(timespec="milliseconds")+"Z" if value is not None else None


def window(start,end):
    try:
        a,b=_stamp(start),_stamp(end)
        if not timedelta(0)<b-a<=timedelta(days=91):raise ValueError()
        return a,b
    except (ValueError,TypeError,OverflowError,InvalidStamp):raise BadRequest("invalid_earnings_window","Use a supported reporting window with explicit timestamps.") from None


def safe_integer(value):
    if type(value) is not int or abs(value)>2**53-1:raise Unavailable("earnings_value_unavailable","A recorded financial value exceeds the display budget.")
    return value


async def read_earnings(session,*,tenant_id,start,end):
    begin,finish=window(start,end)
    await session.connection(execution_options={"isolation_level":"REPEATABLE READ"})
    await session.execute(text("SET LOCAL statement_timeout='3000ms'"))
    now=(await session.scalar(select(func.transaction_timestamp()))).replace(tzinfo=None)
    if finish>now+timedelta(minutes=5):raise BadRequest("invalid_earnings_window","The reporting window is in the future.")
    query=select(OfCreatorBinding).where(OfCreatorBinding.channel=="onlyfans",OfCreatorBinding.revoked_at.is_(None))
    if tenant_id is not None:query=query.where(OfCreatorBinding.tenant_id==tenant_id)
    bindings=list(await session.scalars(query.order_by(OfCreatorBinding.account_id).limit(1001).with_for_update(read=True)))
    ids=[r.account_id for r in bindings]
    if len(ids)>1000 or len(set(ids))!=len(ids):raise Unavailable("earnings_scope_unavailable","The financial account scope needs review.")
    result={"schemaVersion":SCHEMA,"window":{"start":stamp(begin),"end":stamp(finish)},"asOf":stamp(now),
        "accountCount":len(ids),"coveredAccountCount":0,"status":"unavailable","reasons":[],"currency":"USD",
        "clearedGrossCents":None,"clearedNetCents":None,"clearedCount":None,"pendingCount":None,"refundedCount":None,
        "recentLedgerAt":None,"fullLedgerAt":None}
    if not ids:result["reasons"]=["no_bound_accounts"];return result
    rows=(await session.execute(select(OfLedgerScan,OfRuntime.binding_id.label("runtime_binding"),
        OfProviderConnection.binding_id.label("connection_binding"),OfProviderConnection.owner_id.label("connection_owner"),
        OfProviderConnection.revision.label("connection_revision"))
        .outerjoin(OfRuntime,OfRuntime.account_id==OfLedgerScan.account_id)
        .outerjoin(OfProviderConnection,OfProviderConnection.account_id==OfLedgerScan.account_id)
        .where(OfLedgerScan.account_id.in_(ids)))).all()
    scans={row[0].account_id:row for row in rows};reasons=set();recent=[];full=[]
    for binding in bindings:
        data=scans.get(binding.account_id)
        if data is None:reasons.add("ledger_unavailable");continue
        scan,runtime_binding,connection_binding,owner,revision=data
        if (scan.binding_id!=binding.id or runtime_binding!=binding.id or connection_binding!=binding.id
                or owner!=scan.owner_id or revision!=scan.connection_revision or scan.last_complete_at is None or scan.last_full_at is None):
            reasons.add("ledger_unavailable");continue
        recent.append(scan.last_complete_at);full.append(scan.last_full_at)
        if scan.status=="held":reasons.add("ledger_held");continue
        if (not timedelta(0)<=now-scan.last_complete_at<=timedelta(minutes=15)
                or not timedelta(0)<=now-scan.last_full_at<=timedelta(hours=26)):
            reasons.add("ledger_stale");continue
        if begin<scan.last_full_at-timedelta(days=90):reasons.add("outside_coverage");continue
        result["coveredAccountCount"]+=1
    if len(recent)==len(ids):result["recentLedgerAt"]=stamp(min(recent));result["fullLedgerAt"]=stamp(min(full))
    if reasons:result["reasons"]=sorted(reasons);return result
    # Existing provider-ID index makes this evidence check bounded per
    # transaction. Multiple observations never multiply one canonical payment.
    applied=select(OfLedgerEvidence.evidence_hash).where(OfLedgerEvidence.account_id==Transaction.account_id,
        OfLedgerEvidence.provider_transaction_id==Transaction.provider_transaction_id,
        OfLedgerEvidence.transaction_id==Transaction.id,OfLedgerEvidence.outcome=="applied").exists()
    unknown=await session.scalar(select(Transaction.id).where(Transaction.account_id.in_(ids),
        Transaction.occurred_at>=begin,Transaction.occurred_at<finish,Transaction.kind!="ppv_pending",~applied).limit(1))
    if unknown is not None:result["reasons"]=["unverified_ledger_rows"];return result
    groups=(await session.execute(select(Transaction.currency,Transaction.status,func.count(),func.sum(Transaction.amount_cents),
        func.sum(Transaction.net_cents),func.count(Transaction.net_cents)).where(Transaction.account_id.in_(ids),
        Transaction.occurred_at>=begin,Transaction.occurred_at<finish,applied).group_by(Transaction.currency,Transaction.status).limit(16))).all()
    if any(currency!="USD" for currency,*_ in groups):result["reasons"]=["unsupported_currency"];return result
    if any(status not in {"cleared","pending","refund_pending","refunded","chargedback"} for _,status,*_ in groups):
        result["reasons"]=["unsupported_status"];return result
    values={status:(count,gross,net,net_count) for _,status,count,gross,net,net_count in groups}
    cleared,gross,net,net_count=values.get("cleared",(0,0,0,0))
    result.update(status="current",clearedGrossCents=safe_integer(int(gross)),
        clearedNetCents=safe_integer(int(net or 0)) if net_count==cleared else None,
        clearedCount=safe_integer(cleared),pendingCount=safe_integer(sum(values.get(k,(0,))[0] for k in ("pending","refund_pending"))),
        refundedCount=safe_integer(sum(values.get(k,(0,))[0] for k in ("refunded","chargedback"))))
    return result
