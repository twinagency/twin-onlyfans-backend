"""Read-only, tenant-bound totals from canonical native ledger observations.

Amounts are not offers, opens or fan balances. A missing baseline is unknown,
never zero. Refund/pending states stay separate from cleared gross receipts.
"""
import re
from datetime import datetime,timedelta
from sqlalchemy import select,func,and_,or_,text,cast,String
from sqlalchemy.dialects.postgresql import JSONB
from db.models import Transaction,OfCreatorBinding
from db.onlyfans_models import OfRuntime
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_ledger_models import OfLedgerEvidence,OfLedgerScan,OfLedgerHistory
from onlyfans_runtime.text_delivery import _stamp
from onlyfans_runtime.store import Refused as InvalidStamp
from .errors import BadRequest,Unavailable
from transaction_contract import STATUSES
from onlyfans_runtime.ledger import coverage_proven
from onlyfans_runtime.launch_operations_models import OfLaunchCommand,OfLedgerResolution

SCHEMA="of-earnings.v1"
REASONS={"no_bound_accounts","ledger_unavailable","ledger_stale","ledger_held","outside_coverage","unsupported_currency","unsupported_status","unverified_ledger_rows"}


def stamp(value):return value.isoformat(timespec="milliseconds")+"Z" if value is not None else None


def window(start,end):
    try:
        a,b=_stamp(start),_stamp(end)
        if a<datetime(1970,1,1) or not timedelta(0)<b-a<=timedelta(days=60000):raise ValueError()
        return a,b
    except (ValueError,TypeError,OverflowError,InvalidStamp):raise BadRequest("invalid_earnings_window","Use a supported reporting window with explicit timestamps.") from None


def safe_integer(value):
    if type(value) is not int or abs(value)>2**53-1:raise Unavailable("earnings_value_unavailable","A recorded financial value exceeds the display budget.")
    return value


_SCOPE_ID=re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def parse_scope(raw):
    """`accountIds` query value (comma separated) to a sorted unique list, or None when not supplied."""
    if raw is None:return None
    ids=raw.split(",")
    if not 1<=len(ids)<=50 or any(not _SCOPE_ID.match(i) for i in ids):raise BadRequest("invalid_earnings_scope","Supply up to 50 valid account ids.")
    return sorted(set(ids))


def scope_bindings(bindings,account_ids):
    """Authorized bindings intersect the requested ids. An unknown id selects nothing, never the tenant's first account."""
    if account_ids is None:return list(bindings)
    wanted=set(account_ids)
    return [b for b in bindings if b.account_id in wanted]


async def read_earnings(session,*,tenant_id,start,end,include_pending=False,include_history=False,account_ids=None):
    begin,finish=window(start,end)
    await session.connection(execution_options={"isolation_level":"REPEATABLE READ"})
    await session.execute(text("SET LOCAL statement_timeout='3000ms'"))
    now=(await session.scalar(select(func.transaction_timestamp()))).replace(tzinfo=None)
    if finish>now+timedelta(minutes=5):raise BadRequest("invalid_earnings_window","The reporting window is in the future.")
    query=select(OfCreatorBinding).where(OfCreatorBinding.channel=="onlyfans",OfCreatorBinding.revoked_at.is_(None))
    from .agency_scope import current_scope
    scope = current_scope.get()
    if scope is not None:query=query.where(OfCreatorBinding.creator_id.in_(scope.creator_ids))
    if tenant_id is not None:query=query.where(OfCreatorBinding.tenant_id==tenant_id)
    bindings=list(await session.scalars(query.order_by(OfCreatorBinding.account_id).limit(1001).with_for_update(read=True)))
    bindings=scope_bindings(bindings,account_ids)
    ids=[r.account_id for r in bindings]
    if len(ids)>1000 or len(set(ids))!=len(ids):raise Unavailable("earnings_scope_unavailable","The financial account scope needs review.")
    result={"schemaVersion":SCHEMA,"window":{"start":stamp(begin),"end":stamp(finish)},"asOf":stamp(now),
        "accountCount":len(ids),"coveredAccountCount":0,"status":"unavailable","reasons":[],"currency":"USD",
        "clearedGrossCents":None,"clearedNetCents":None,"clearedCount":None,"pendingCount":None,"refundedCount":None,
        "recentLedgerAt":None,"fullLedgerAt":None}
    if account_ids is not None:result["scope"]={"accountIds":sorted(ids)}  # echo what was applied; absent means agency-wide
    if include_pending:
        result["recorded"]=None
    if include_history:
        result["statistics"]=None
        result["history"]={"accountCount":len(ids),"completedAccountCount":0,"accounts":[]}
    if not ids:result["reasons"]=["no_bound_accounts"];return result
    rows=(await session.execute(select(OfLedgerScan,OfRuntime.binding_id.label("runtime_binding"),
        OfProviderConnection.binding_id.label("connection_binding"),OfProviderConnection.owner_id.label("connection_owner"),
        OfProviderConnection.revision.label("connection_revision"))
        .outerjoin(OfRuntime,OfRuntime.account_id==OfLedgerScan.account_id)
        .outerjoin(OfProviderConnection,OfProviderConnection.account_id==OfLedgerScan.account_id)
        .where(OfLedgerScan.account_id.in_(ids)))).all()
    histories={row.account_id:row for row in await session.scalars(
        select(OfLedgerHistory).where(OfLedgerHistory.account_id.in_(ids)))}
    scans={row[0].account_id:row for row in rows};reasons=set();recent=[];full=[];coverage=[]
    if include_history:
        for binding in bindings:
            h=histories.get(binding.account_id);data=scans.get(binding.account_id)
            valid=(h is not None and data is not None and
                (h.binding_id,h.owner_id,h.connection_revision)==(binding.id,data[3],data[4])
                and data[1]==binding.id and data[2]==binding.id)
            complete=bool(valid and coverage_proven(h) and h.last_full_at and h.status!="held")
            state="complete" if complete else "held" if valid and h.status=="held" else "syncing" if valid else "unavailable"
            result["history"]["completedAccountCount"]+=int(complete)
            result["history"]["accounts"].append({"accountId":binding.account_id,"status":state,
                "importedPayments":h.page_offset if valid else 0,
                "lastSuccessfulSyncAt":stamp(h.last_complete_at) if valid else None})

    for binding in bindings:
        data=scans.get(binding.account_id)
        if data is None:reasons.add("ledger_unavailable");continue
        scan,runtime_binding,connection_binding,owner,revision=data
        if (scan.binding_id!=binding.id or runtime_binding!=binding.id or connection_binding!=binding.id
                or owner!=scan.owner_id or revision!=scan.connection_revision or not coverage_proven(scan) or scan.last_full_at is None):
            reasons.add("ledger_unavailable");continue
        h=histories.get(binding.account_id)
        history_current=(h is not None and (h.binding_id,h.owner_id,h.connection_revision)==
            (binding.id,owner,revision) and coverage_proven(h) and h.last_full_at is not None
            and h.window_start==datetime(1970,1,1) and h.status!="held"
            and timedelta(0)<=now-h.last_full_at<=timedelta(hours=26))
        baseline=h.last_full_at if history_current else scan.last_full_at
        floor=datetime(1970,1,1) if history_current else baseline-timedelta(days=90)
        recent.append(scan.last_complete_at);full.append(baseline);coverage.append(floor)
        if scan.status=="held":reasons.add("ledger_held");continue
        if (not timedelta(0)<=now-scan.last_complete_at<=timedelta(minutes=15)
                or not timedelta(0)<=now-baseline<=timedelta(hours=26)):
            reasons.add("ledger_stale");continue
        if begin<floor:reasons.add("outside_coverage");continue
        result["coveredAccountCount"]+=1
    if len(recent)==len(ids):result["recentLedgerAt"]=stamp(min(recent));result["fullLedgerAt"]=stamp(min(full))
    result["reasons"]=sorted(reasons)
    # A delayed poll does not erase observed sales. Optional recorded totals
    # retain their own last-success timestamp and never become current totals.
    recorded_coverage=(include_pending and len(recent)==len(ids)
        and not reasons-{"ledger_stale","ledger_held"}
        and all(timedelta(0)<=now-value<=timedelta(hours=24) for value in recent)
        and all(timedelta(0)<=now-value<=timedelta(hours=26) for value in full)
        and all(begin>=floor for floor in coverage))
    if reasons and not recorded_coverage:return result
    # Existing provider-ID index makes this evidence check bounded per
    # transaction. Multiple observations never multiply one canonical payment.
    applied=select(OfLedgerEvidence.evidence_hash).where(OfLedgerEvidence.account_id==Transaction.account_id,
        OfLedgerEvidence.provider_transaction_id==Transaction.provider_transaction_id,
        OfLedgerEvidence.transaction_id==Transaction.id,OfLedgerEvidence.outcome=="applied").exists()
    # Legacy conflicts can be reprocessed by the existing audited action while
    # message history is still missing. These receipts prove money independently
    # of a later media-link decision; no observation or purchase right is forged.
    decision=cast(OfLaunchCommand.result_json,JSONB)
    replayed=select(OfLedgerEvidence.evidence_hash).join(OfLaunchCommand,and_(
        OfLaunchCommand.account_id==OfLedgerEvidence.account_id,
        decision["evidenceHash"].astext==OfLedgerEvidence.evidence_hash)).where(
        OfLedgerEvidence.account_id==Transaction.account_id,
        OfLedgerEvidence.provider_transaction_id==Transaction.provider_transaction_id,
        OfLaunchCommand.kind=="ledger_resolution",decision["financialRecorded"].astext=="true",
        decision["transactionId"].astext==cast(Transaction.id,String)).exists()
    resolved=select(OfLedgerResolution.transaction_id).where(
        OfLedgerResolution.account_id==Transaction.account_id,
        OfLedgerResolution.transaction_id==Transaction.id).exists()
    applied=or_(applied,replayed,resolved)
    financial_decision=select(OfLaunchCommand.command_key).join(Transaction,and_(
        Transaction.account_id==OfLaunchCommand.account_id,
        decision["transactionId"].astext==cast(Transaction.id,String))).where(
        OfLaunchCommand.account_id==OfLedgerEvidence.account_id,OfLaunchCommand.kind=="ledger_resolution",
        decision["evidenceHash"].astext==OfLedgerEvidence.evidence_hash,
        decision["financialRecorded"].astext=="true",
        Transaction.provider_transaction_id==OfLedgerEvidence.provider_transaction_id).correlate(OfLedgerEvidence).exists()
    resolved_conflict=select(OfLedgerResolution.evidence_hash).where(
        OfLedgerResolution.account_id==OfLedgerEvidence.account_id,
        OfLedgerResolution.evidence_hash==OfLedgerEvidence.evidence_hash).correlate(OfLedgerEvidence).exists()
    conflict=await session.scalar(select(OfLedgerEvidence.evidence_hash).where(
        OfLedgerEvidence.account_id.in_(ids),OfLedgerEvidence.outcome=="conflict",
        ~resolved_conflict,~financial_decision).limit(1))
    if conflict is not None:result["reasons"]=["unverified_ledger_rows"];return result
    unknown=await session.scalar(select(Transaction.id).where(Transaction.account_id.in_(ids),
        Transaction.occurred_at>=begin,Transaction.occurred_at<finish,Transaction.kind!="ppv_pending",~applied).limit(1))
    if unknown is not None:result["reasons"]=["unverified_ledger_rows"];return result
    groups=(await session.execute(select(Transaction.currency,Transaction.status,func.count(),func.sum(Transaction.amount_cents),
        func.sum(Transaction.net_cents),func.count(Transaction.net_cents)).where(Transaction.account_id.in_(ids),
        Transaction.occurred_at>=begin,Transaction.occurred_at<finish,applied).group_by(Transaction.currency,Transaction.status).limit(16))).all()
    if any(currency!="USD" for currency,*_ in groups):result["reasons"]=["unsupported_currency"];return result
    if any(status not in STATUSES for _,status,*_ in groups):
        result["reasons"]=["unsupported_status"];return result
    values={status:(count,gross,net,net_count) for _,status,count,gross,net,net_count in groups}
    cleared,gross,net,net_count=values.get("cleared",(0,0,0,0))
    if include_pending:
        pending_count,pending_gross,pending_net,pending_net_count=values.get("pending",(0,0,0,0))
        result["recorded"]={
            "grossCents":safe_integer(int(gross)+int(pending_gross)),
            "netCents":safe_integer(int(net or 0)+int(pending_net or 0)) if net_count==cleared and pending_net_count==pending_count else None,
            "pendingGrossCents":safe_integer(int(pending_gross)),
            "pendingNetCents":safe_integer(int(pending_net or 0)) if pending_net_count==pending_count else None,
            "paymentCount":safe_integer(cleared+pending_count),
            "lastSuccessfulSyncAt":result["recentLedgerAt"],
        }
    if include_history and include_pending:
        eligible=Transaction.status.in_(("cleared","pending"))
        stats=(await session.execute(select(Transaction.account_id,Transaction.kind,
            func.count(),func.sum(Transaction.amount_cents),func.sum(Transaction.net_cents),
            func.count(Transaction.net_cents)).where(Transaction.account_id.in_(ids),
            Transaction.occurred_at>=begin,Transaction.occurred_at<finish,applied,eligible)
            .group_by(Transaction.account_id,Transaction.kind).limit(20001))).all()
        if len(stats)>20000:raise Unavailable("earnings_value_unavailable","Financial breakdown exceeds the display budget.")
        kinds={};accounts={}
        for account,kind,n,stat_gross,stat_net,nnet in stats:
            for key,target in ((kind,kinds),(account,accounts)):
                value=target.setdefault(key,{"paymentCount":0,"grossCents":0,"netCents":0})
                value["paymentCount"]+=int(n);value["grossCents"]+=int(stat_gross)
                value["netCents"]=None if value["netCents"] is None or nnet!=n else value["netCents"]+int(stat_net or 0)
        result["statistics"]={"byKind":[{"kind":k,**v} for k,v in sorted(kinds.items())],
            "byAccount":[{"accountId":k,**v} for k,v in sorted(accounts.items())]}
        for group in result["statistics"].values():
            for value in group:
                for field in ("paymentCount","grossCents","netCents"):
                    if value[field] is not None:safe_integer(value[field])
    if reasons:return result
    result.update(status="current",clearedGrossCents=safe_integer(int(gross)),
        clearedNetCents=safe_integer(int(net or 0)) if net_count==cleared else None,
        clearedCount=safe_integer(cleared),pendingCount=safe_integer(sum(values.get(k,(0,))[0] for k in ("pending","refund_pending"))),
        refundedCount=safe_integer(sum(values.get(k,(0,))[0] for k in ("refunded","chargedback"))))
    return result
