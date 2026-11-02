"""Read-only private snapshots separate reused work from a subsequent pilot run.

Snapshots contain source fingerprints and call IDs, never message text. Keep them
on the operational host. No model/provider requests, activation, DDL or deletion.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from db.models import Message, GrokCall
from db.onlyfans_models import OfIngress
from onlyfans_runtime.history_policy import policy
from onlyfans_runtime.history_telemetry import OfHistoryReadAttempt
from onlyfans_runtime.profile_learning.accounting import metadata
from onlyfans_runtime.store import Store, Refused

SCHEMA = 'of-history-pilot-snapshot.v1'


def capture(store, tenant, account):
    with store.engine.connect().execution_options(isolation_level='REPEATABLE READ') as connection:
        with Session(bind=connection) as s, s.begin():
            runtime = store._runtime(s, tenant, account, lock=False)
            p = policy(s, account, runtime.binding_id)
            if p is None: raise Refused('recent-history policy is not active')
            scope = [Message.account_id == account, Message.created_at >= p.cutoff_at,
                select(OfIngress.event_key).where(OfIngress.account_id == account,
                    OfIngress.binding_id == runtime.binding_id, OfIngress.status == 'applied',
                    OfIngress.fan_id == Message.fan_id, OfIngress.message_id == Message.message_id).exists()]
            sources = {}
            for fan, mid, at, direction, unsent, automation, body_hash in s.execute(select(
                    Message.fan_id, Message.message_id, Message.created_at, Message.direction,
                    Message.is_unsent, Message.automation_kind, func.md5(Message.body)).where(*scope)):
                key = hashlib.sha256(f'{account}:{fan}:{mid}'.encode()).hexdigest()
                digest = hashlib.sha256(json.dumps([str(at), direction, unsent, automation or '', body_hash]).encode()).hexdigest()
                sources[key] = digest
            # The same fingerprint conditions used by the paid learner and progress report.
            receipts = {}
            for fan, mid, at in s.execute(text('''SELECT m.fan_id,m.message_id,r.processed_at
                FROM messages m JOIN of_profile_receipts r ON r.account_id=m.account_id
                AND r.binding_id=:binding AND r.fan_id=m.fan_id AND r.source_fan=m.fan_id
                AND r.message_id=m.message_id AND r.body_md5=md5(m.body) AND r.source_at=m.created_at
                AND r.direction=m.direction AND r.unsent=m.is_unsent
                AND r.automation=coalesce(m.automation_kind,'') AND r.next_chunk=r.total_chunks
                WHERE m.account_id=:account AND m.created_at>=:cutoff AND m.direction='in'
                AND EXISTS (SELECT 1 FROM of_ingress i WHERE i.account_id=m.account_id
                    AND i.binding_id=:binding AND i.fan_id=m.fan_id AND i.message_id=m.message_id
                    AND i.status='applied')'''), {'account':account,'binding':runtime.binding_id,'cutoff':p.cutoff_at}):
                key = hashlib.sha256(f'{account}:{fan}:{mid}'.encode()).hexdigest()
                receipts[key] = {'digest': sources[key], 'committedAt': str(at)}
            calls = {}
            for call in s.scalars(select(GrokCall).where(GrokCall.account_id == account, GrokCall.purpose == 'gen_info')):
                billing = metadata(call).get('billing')
                bucket = ('activeReservationUnits' if call.status == 'pending' else
                    'unresolvedReservationUnits' if call.status == 'unknown' else
                    'providerReportedUnits' if billing in {'provider_reported_cost','provider_reconciled'} else 'tokenEstimateUnits')
                calls[str(call.id)] = {'bucket':bucket,'units':call.cost_cents or 0,'latencyMs':call.latency_ms or 0}
            attempts = s.scalar(select(func.coalesce(func.sum(OfHistoryReadAttempt.http_attempts),0)).where(
                OfHistoryReadAttempt.account_id == account, OfHistoryReadAttempt.binding_id == runtime.binding_id,
                OfHistoryReadAttempt.scope == 'recent'))
            return {'schemaVersion':SCHEMA,'observedAt':datetime.now(timezone.utc).isoformat(),
                'accountId':account,'bindingId':runtime.binding_id,'cutoffAt':p.cutoff_at.isoformat(),
                'sources':sources,'receipts':receipts,'modelCalls':calls,'recentHttpAttempts':int(attempts)}


def compare(before, after):
    for key in ['schemaVersion','accountId','bindingId','cutoffAt']:
        if before.get(key) != after.get(key): raise ValueError('snapshot scope changed: '+key)
    if before.get('schemaVersion') != SCHEMA: raise ValueError('invalid snapshot schema')
    start,end = (datetime.fromisoformat(x['observedAt']) for x in [before,after])
    if end < start: raise ValueError('snapshots reversed')
    old,new = before['sources'],after['sources']
    old_r,new_r = before['receipts'],after['receipts']
    reused = {k for k,v in new.items() if old.get(k) == v}
    completed = {k for k,v in new_r.items() if old_r.get(k,{}).get('digest') != v['digest']}
    changed_old_calls = {k for k,v in after['modelCalls'].items() if k in before['modelCalls'] and before['modelCalls'][k] != v}
    new_calls = {k:v for k,v in after['modelCalls'].items() if k not in before['modelCalls']}
    def totals(calls):
        result = {k:0 for k in ['providerReportedUnits','tokenEstimateUnits','activeReservationUnits','unresolvedReservationUnits']}
        for c in calls: result[c['bucket']] += c['units']
        return result
    committed = [datetime.fromisoformat(new_r[k]['committedAt']) for k in completed]
    previous = totals(before['modelCalls'][k] for k in changed_old_calls)
    current = totals(after['modelCalls'][k] for k in changed_old_calls)
    return {'schemaVersion':'of-history-pilot-comparison.v1','accountId':after['accountId'],
        'basis':'Observed pilot with reused archive; not fresh creator onboarding performance',
        'observedElapsedSeconds':(end-start).total_seconds(),
        'existingWindowSourcesAtStart':len(old),'unchangedExistingWindowSourcesReused':len(reused),
        'newWindowSourcesSaved':len(set(new)-set(old)),
        'existingWindowSourcesEdited':sum(k in old and old[k]!=v for k,v in new.items()),
        'baselineSourcesNoLongerInScope':len(set(old)-set(new)),
        'reusedCompletedIncomingReceipts':sum(old_r.get(k,{}).get('digest')==v['digest'] for k,v in new_r.items()),
        'newlyCompletedIncomingReceipts':len(completed),
        'newlyCompletedReceiptsForReusedSources':len(completed & reused),
        'newReceiptCommitSpanSeconds':(max(committed)-min(committed)).total_seconds() if committed else None,
        'receiptSpanIsNotTotalMemoryProcessingTime':True,
        'newRecentProviderHttpAttempts':after['recentHttpAttempts']-before['recentHttpAttempts'],
        'newModelCalls':len(new_calls),'newModelCallAmounts':totals(new_calls.values()),
        'amountUsdPerUnit':'0.0001',
        'preExistingModelCallSettlementChanges':{k:current[k]-previous[k] for k in current},
        'cumulativeNewModelCallLatencyMs':sum(c['latencyMs'] for c in new_calls.values()),
        'newSourcesIncludeConcurrentLiveArrivals':True,
        'fullHistoryTimeOrCostSavingsDemonstrated':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', required=True)
    parser.add_argument('--save-snapshot', required=True, type=Path)
    parser.add_argument('--compare-with', type=Path)
    args=parser.parse_args()
    before=json.loads(args.compare_with.read_text()) if args.compare_with else None
    store=Store(os.environ['DATABASE_URL'])
    try: snapshot=capture(store,os.environ['ONLYFANS_WORKER_TENANT'],args.account)
    finally: store.close()
    # Exclusive creation prevents accidental destruction of the before evidence.
    fd=os.open(args.save_snapshot,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as stream: json.dump(snapshot,stream)
    print(json.dumps(compare(before,snapshot) if before else {
        'accountId':args.account,'baselineCaptured':True,'windowSources':len(snapshot['sources']),
        'completedIncomingReceipts':len(snapshot['receipts']),'modelCallRecords':len(snapshot['modelCalls'])},indent=2))

if __name__=='__main__': main()
