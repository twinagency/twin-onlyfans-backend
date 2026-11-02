"""Read-only, scoped coverage. Unknown lifetime totals never become percentages."""
from sqlalchemy import and_, func, select, text
from db.models import Message, OfCreatorBinding
from db.onlyfans_models import OfIngress
from .history_policy import policy, OfOlderHistoryJob
from .store import utcnow


def recent_progress(s, runtime, fan_id=None):
    p = policy(s, runtime.account_id, runtime.binding_id)
    if p is None:
        return None
    from .launch_history_memory.recovery import OfRecentHistoryCursor as C, OfRecentHistoryScan
    filters = [C.account_id == runtime.account_id, C.binding_id == runtime.binding_id]
    scope = [Message.account_id == runtime.account_id, Message.created_at >= p.cutoff_at,
        select(OfIngress.event_key).where(OfIngress.account_id == Message.account_id,
            OfIngress.binding_id == runtime.binding_id, OfIngress.fan_id == Message.fan_id,
            OfIngress.message_id == Message.message_id, OfIngress.status == 'applied').exists()]
    if fan_id is not None:
        filters.append(C.fan_id == fan_id)
        scope.append(Message.fan_id == fan_id)
    counts = dict(s.execute(select(C.status, func.count()).where(*filters).group_by(C.status)).all())
    errors = dict(s.execute(select(C.last_error, func.count()).where(*filters,
        C.last_error.is_not(None)).group_by(C.last_error)).all())
    # A thread the provider answers 404/410 for is permanent missing coverage,
    # not a failure that a retry is expected to clear: it is retried hourly
    # (HistoryRecovery.fail) and a later success clears last_error. It stays
    # counted (unavailable / providerErrors) but must not hold readiness
    # forever. Every other error, including conflicting_history, still blocks.
    unavailable = s.scalar(select(func.count()).where(*filters, C.status != 'complete',
        C.last_error == 'provider_thread_unavailable'))
    blocking_errors = {k: v for k, v in errors.items() if k != 'provider_thread_unavailable'}
    awaiting = counts.get('needed', 0) + counts.get('running', 0) - unavailable
    scan = s.get(OfRecentHistoryScan, runtime.account_id)
    discovery = bool(scan and scan.binding_id == runtime.binding_id and scan.status == 'complete'
                     and scan.known_fans_complete and not scan.last_error)
    # This state describes the initial import, including complete discovery.
    # One completed fan cannot make the account window ready while other
    # conversations are still being discovered (or satisfy the UI contract).
    covered = discovery and (bool(counts) if fan_id is not None else True) and not (
        awaiting or blocking_errors)
    total, fans, incoming = s.execute(select(func.count(), func.count(func.distinct(Message.fan_id)),
        func.count().filter(Message.direction == 'in')).where(*scope)).one()
    tenant = s.scalar(select(OfCreatorBinding.tenant_id).where(OfCreatorBinding.id==runtime.binding_id))
    params = {'tenant':tenant,'account':runtime.account_id,'binding':runtime.binding_id,'cutoff':p.cutoff_at,'fan':fan_id}
    # Same source-fingerprint receipts as the paid learner, including fact-free results.
    pending_sources = '''FROM messages m WHERE m.account_id=:account
        AND m.created_at>=:cutoff AND m.direction='in' AND (:fan IS NULL OR m.fan_id=:fan)
        AND EXISTS (SELECT 1 FROM of_ingress i WHERE i.account_id=m.account_id AND i.binding_id=:binding
            AND i.fan_id=m.fan_id AND i.message_id=m.message_id AND i.status='applied')
        AND NOT EXISTS (SELECT 1 FROM of_profile_receipts r WHERE r.tenant_id=:tenant AND r.account_id=m.account_id
            AND r.binding_id=:binding AND r.fan_id=m.fan_id AND r.source_fan=m.fan_id
            AND r.message_id=m.message_id AND r.body_md5=md5(m.body) AND r.source_at=m.created_at
            AND r.direction=m.direction AND r.unsent=m.is_unsent AND r.automation=coalesce(m.automation_kind,'')
            AND r.next_chunk=r.total_chunks)'''
    pending = s.scalar(text('SELECT count(*) ' + pending_sources),params)
    old = s.scalar(select(func.count()).select_from(OfOlderHistoryJob).where(
        OfOlderHistoryJob.account_id == runtime.account_id, OfOlderHistoryJob.binding_id == runtime.binding_id,
        True if fan_id is None else OfOlderHistoryJob.fan_id == fan_id))
    from .launch_history_memory.recovery import OfHistoryCursor as L, OfHistoryScan
    lifetime_scan = s.get(OfHistoryScan, runtime.account_id)
    lifetime_states = dict(s.execute(select(L.status,func.count()).where(
        L.account_id==runtime.account_id,L.binding_id==runtime.binding_id,
        True if fan_id is None else L.fan_id==fan_id).group_by(L.status)).all())
    lifetime_complete = bool(lifetime_states) and not (lifetime_states.get('needed',0) or lifetime_states.get('running',0))
    if fan_id is None:
        from db.models import Fan
        missing = s.scalar(select(func.count()).select_from(Fan).where(Fan.account_id==runtime.account_id,
            ~select(L.fan_id).where(L.account_id==Fan.account_id,L.fan_id==Fan.fan_id,L.binding_id==runtime.binding_id).exists()))
        lifetime_complete = bool(lifetime_complete and lifetime_scan and lifetime_scan.status=='complete'
            and lifetime_scan.known_fans_complete and not missing)
    older_done = s.scalar(select(func.count()).select_from(OfOlderHistoryJob).join(L,and_(
        L.account_id==OfOlderHistoryJob.account_id,L.fan_id==OfOlderHistoryJob.fan_id,
        L.binding_id==OfOlderHistoryJob.binding_id)).where(OfOlderHistoryJob.account_id==runtime.account_id,
        OfOlderHistoryJob.binding_id==runtime.binding_id,L.status=='complete',
        True if fan_id is None else OfOlderHistoryJob.fan_id==fan_id))
    hold = s.execute(text('''SELECT hold_code,hold_until FROM of_profile_accounts
        WHERE tenant_id=:tenant AND account_id=:account AND binding_id=:binding'''),params).first()
    active_hold = bool(hold and hold[1] and str(hold[1]) > str(utcnow()))
    # Historical failures remain stored, but only failures with unprocessed
    # current-window sources can block recent memory readiness.
    memory_errors, deferred_errors = s.execute(text(f"""SELECT
        count(*) FILTER (WHERE fan_id IN (SELECT m.fan_id {pending_sources})),
        count(*) FILTER (WHERE fan_id NOT IN (SELECT m.fan_id {pending_sources}))
        FROM of_profile_learning WHERE tenant_id=:tenant AND account_id=:account
        AND binding_id=:binding AND fan_id>0 AND (:fan IS NULL OR fan_id=:fan)
        AND error_code IS NOT NULL"""),params).one()
    from .history_telemetry import OfHistoryReadAttempt as A
    attempts, http_attempts, failed, unfinished = s.execute(select(func.count(),
        func.coalesce(func.sum(A.http_attempts),0),func.count().filter(A.outcome=='failed'),
        func.count().filter(A.outcome=='unfinished')).where(A.account_id==runtime.account_id,
            A.binding_id==runtime.binding_id,A.scope=='recent',
            True if fan_id is None else A.fan_id==fan_id)).one()
    from db.models import GrokCall
    from .profile_learning.accounting import metadata
    from decimal import Decimal
    billing = {k:0 for k in ['providerReportedUnits','tokenEstimateUnits','activeReservationUnits','unresolvedReservationUnits']}
    model_ms = 0
    for call in s.scalars(select(GrokCall).where(GrokCall.account_id==runtime.account_id,
            GrokCall.purpose=='gen_info',GrokCall.called_at>=p.started_at,
            True if fan_id is None else GrokCall.fan_id==fan_id)):
        meta = metadata(call)
        bucket = ('activeReservationUnits' if call.status=='pending' else
            'unresolvedReservationUnits' if call.status=='unknown' else
            'providerReportedUnits' if meta.get('billing') in {'provider_reported_cost','provider_reconciled'} else 'tokenEstimateUnits')
        billing[bucket] += call.cost_cents or 0
        model_ms += call.latency_ms or 0
    accounting = {k.replace('Units','Usd'):str(Decimal(v)/10000) for k,v in billing.items()}
    receipt_first, receipt_last = s.execute(text("""SELECT min(processed_at),max(processed_at) FROM of_profile_receipts
        WHERE tenant_id=:tenant AND account_id=:account AND binding_id=:binding AND fan_id>0
        AND source_at>=:cutoff AND processed_at>=:started AND (:fan IS NULL OR fan_id=:fan)"""),{**params,'started':p.started_at}).one()
    accounting.update(scope='account memory calls since fixed policy start, including activated older jobs',
        cumulativeModelCallMilliseconds=model_ms)
    completed_at = s.scalar(select(func.max(C.completed_at)).where(*filters)) if covered else None
    from .history_policy import approved_onboarding
    onboarding = approved_onboarding(s, runtime.account_id, runtime.binding_id)
    return {'creatorOnboarding':{'state':'approved' if onboarding else 'review_required','routineCreatorArchiveExtraction':'disabled'},
        'schemaVersion':'of-recent-history.v1', 'startedAt':p.started_at.isoformat()+'Z',
        'cutoffAt':p.cutoff_at.isoformat()+'Z', 'retentionPolicy':'preserve_all_stored_data',
        'initialHistory':{'state':'recent_history_ready' if covered else 'incomplete',
            'discoveryComplete':discovery,'discoveryPasses':scan.pass_number if scan else 0,
            'discoveryError':scan.last_error if scan else None,
            'conversationsDiscovered':sum(counts.values()),'conversationsReady':counts.get('complete',0),
            'conversationsAwaitingWork':counts.get('needed',0)+counts.get('running',0),
            'conversationsUnavailable':unavailable,'blockingProviderErrors':blocking_errors,
            'messagesSavedInWindow':total,'fansWithStoredWindowMessages':fans,'providerErrors':errors,
            'completedAt':completed_at.isoformat()+'Z' if completed_at else None,
            'elapsedSeconds':max(0,((completed_at or utcnow())-p.started_at).total_seconds()),
            'providerOrdering':'verified_timestamp_desc' if p.timestamp_order_verified else 'id_desc_exhaustive_date_filter',
            'orderingEvidence':p.ordering_evidence,
            'providerRequests':int(http_attempts), 'pageReadAttempts':attempts, 'failedReadAttempts':failed,
            'unfinishedReadAttempts':unfinished, 'providerRequestMeasurement':'HTTP attempts counted at dispatch, including identity probes and failures'},
        'memoryProcessing':{'state':'ready' if covered and pending==0 and not active_hold and not memory_errors else 'incomplete',
            'incomingMessagesInWindow':incoming,'messagesProcessed':incoming-pending,'messagesAwaitingProcessing':pending,
            'firstCurrentRunReceiptAt':str(receipt_first) if receipt_first else None,
            'lastCurrentRunReceiptAt':str(receipt_last) if receipt_last else None,
            'receiptIntervalMeasurement':'wall interval between committed memory receipts; cumulative model latency is separate',
            'errors':memory_errors, 'errorsOutsidePendingRecentWork':deferred_errors, 'budgetHold':hold[0] if active_hold else None,
            'completionDoesNotMeanLifetimeHistory':True},
        'memoryModelAccounting':accounting,
        'lifetimeHistory':{'complete':lifetime_complete,'conversationsComplete':lifetime_states.get('complete',0)},
        'olderHistory':{'activatedFanJobs':old,'completedFanJobs':older_done,'pendingFanJobs':old-older_done,'scheduler':'bounded existing archive lane'},
        'lifetimeTotalMessages':None,'percentComplete':None}
