"""Scoped projections shared by the inbox, native planner and FASTT prompt hook."""
from datetime import datetime, timedelta, timezone
import json
import os

from sqlalchemy import select, text
from db.models import Fan, Message, OfCreatorBinding
from db.onlyfans_models import OfRuntime
from .evidence import FIELD_LOCKS, digest, norm, project, relevance, select_memories, summary, subject_supported, quality_fact

SCOPE = 'tenant_id=:tenant_id AND account_id=:account_id AND binding_id=:binding_id AND fan_id=:fan_id'


def identity(tenant, account, binding, fan):
    return dict(tenant_id=tenant,account_id=account,binding_id=binding,fan_id=fan)


def _binding_scope(s, account):
    return s.execute(select(OfRuntime.binding_id,OfCreatorBinding.tenant_id).join(
        OfCreatorBinding,OfCreatorBinding.id==OfRuntime.binding_id).where(
        OfRuntime.account_id==account,OfCreatorBinding.account_id==account,
        OfCreatorBinding.channel=='onlyfans',OfCreatorBinding.revoked_at.is_(None))).first()


def _learning_state(row, *, now=None):
    """Stored-scope coverage, independent of whether relevant facts were found.

    This is shared by the inbox and reply projection. A failed refresh cannot
    advertise completed coverage merely because an older document did so.
    """
    try:
        document=json.loads(row['document_json']) if row else {}
        if type(document) is not dict: raise ValueError('invalid coverage document')
    except (ValueError,TypeError):
        return {},'invalid_coverage'
    if document.get('processing_scope','all_stored_history') not in (
            'all_stored_history','recent_window_and_activated_older'):
        return document,'invalid_coverage'
    queued=row['queued_at'] if row else None
    if isinstance(queued,str): queued=datetime.fromisoformat(queued)
    evaluated=datetime.fromisoformat(str(now).replace('Z','+00:00')) if now is not None else datetime.utcnow()
    if evaluated.tzinfo is not None: evaluated=evaluated.astimezone(timezone.utc).replace(tzinfo=None)
    if queued is not None and queued.tzinfo is not None:
        queued=queued.astimezone(timezone.utc).replace(tzinfo=None)
    pending=bool(queued and queued<=evaluated)
    state=('failed' if row and row['error_code'] else
           'not_processed' if not row or not row['checked_at'] else
           'processing_history' if document.get('history_complete') is not True else
           'processing_updates' if pending else 'ready')
    return document,state


def _learning_availability(row, account, *, now=None):
    from . import cohort
    from onlyfans_runtime.store import Refused
    document,state=_learning_state(row,now=now)
    scope_error=None
    try:
        selected=cohort.includes(account)
    except Refused:
        # Invalid worker configuration is an operational problem, not missing
        # personal evidence or a reason to stop an otherwise eligible reply.
        selected=None
        scope_error='invalid-profile-account-scope'
    complete=state=='ready'
    availability={'workerSelected':selected,'state':state,
        'processingScope':document.get('processing_scope','all_stored_history') if row else None,
        'scopedHistoryProcessed':complete,
        'storedHistoryProcessed':complete and document.get('processing_scope')!='recent_window_and_activated_older'}
    if scope_error:
        availability['workerScopeError']=scope_error
    return document,availability


def valid_evidence(s, account, binding, evidence):
    from db.onlyfans_models import OfIngress
    try:
        fid,mid=map(int,evidence['source'].split(':'))
    except (ValueError,KeyError,AttributeError):
        return False
    m=s.get(Message,(account,fid,mid))
    proof=s.scalar(select(OfIngress.event_key).where(OfIngress.account_id==account,
        OfIngress.binding_id==binding,OfIngress.fan_id==fid,OfIngress.message_id==mid,
        OfIngress.status=='applied').limit(1))
    return bool(m is not None and proof and not m.is_unsent and
        digest([m.body,m.is_unsent,m.direction,m.created_at.isoformat(),m.automation_kind])==evidence['digest']
        and evidence['quote'] in m.body)


def valid_observation(s, account, binding, observation):
    return valid_evidence(s,account,binding,observation) and all(
        valid_evidence(s,account,binding,e) for e in observation.get('context_evidence',[]))


def load_memories(s, account, fan, *, query='', now=None):
    """Must be called under the caller's existing account/tenant access lock.

    A fresh current-binding lookup prevents stale ownership and removed evidence
    from reaching either UI or a prompt, even before the learner refreshes.
    """
    scope=_binding_scope(s,account)
    if not scope: return []
    binding,tenant=scope
    params=identity(tenant,account,binding,fan)
    # Materialized memory rows are archival storage; filter/rank before loading bounded candidates.
    clauses=[]
    words=[w for w in norm(query).split() if len(w)>2 or w.isdigit()][:8]
    for i,w in enumerate(words):
        params['word'+str(i)]='%'+w+'%'
        clauses.append('lower(document_json) LIKE :word'+str(i))
    match=' OR '.join(clauses) or 'false'
    rows=s.execute(text(f'''SELECT document_json FROM of_profile_memories WHERE {SCOPE}
        AND (status NOT IN ('expired','superseded','cancelled','invalidated') OR ({match}))
        ORDER BY CASE WHEN ({match}) THEN 0 ELSE 1 END,
        CASE field WHEN 'name' THEN 0 WHEN 'occupation' THEN 1 WHEN 'interests' THEN 2 ELSE 3 END,
        CASE WHEN kind='request' AND status NOT IN ('past','cancelled') THEN 0 ELSE 1 END,
        memory_key LIMIT 256'''),params).scalars()
    from onlyfans_runtime.launch_history_memory.memory import locked_fields
    locks=locked_fields(s,account,fan) if fan else set()
    manual_rows=s.execute(text(f'SELECT memory_key,document_json FROM of_profile_manual WHERE {SCOPE} AND protected=true'),params).all()
    manual={key:json.loads(raw) for key,raw in manual_rows}
    facts=list(manual.values())
    candidates=[]
    for raw in rows:
        f=json.loads(raw)
        if f['memory_key'] in manual or locks & FIELD_LOCKS.get(f['field'],set()): continue
        if not subject_supported(f, creator=not bool(fan)): continue
        if quality_fact(f) is None: continue
        rank=relevance(f,query,now)
        if rank is not None: candidates.append((rank[0],f))
    candidates.sort(key=lambda item:(-item[0],item[1]['memory_key']))
    # At most 24 candidate proofs per reply. Repeated mentions do not create
    # thousands of provenance reads in the synchronous reply path.
    proofs=request_proofs=0
    for _,f in candidates:
        if f['kind']=='request' and request_proofs>=6: continue
        if proofs>=24: break
        proofs+=1
        request_proofs+=f['kind']=='request'
        if not valid_observation(s,account,binding,f): continue
        if any(not valid_observation(s,account,binding,c) for c in f.get('conflicts',[])):
            continue  # Rebuild uncertain/corrected history in the learner before use.
        facts.append(f)
    return facts


def reply_memory(s, account, fan, *, query='', now=None):
    if os.environ.get('ONLYFANS_PROFILE_LEARNING_ENABLED')!='1': return None
    from onlyfans_runtime.history_policy import memory_notice
    scope=_binding_scope(s,account)
    row=None
    if scope:
        binding,tenant=scope
        row=s.execute(text(f'SELECT document_json,checked_at,queued_at,error_code FROM of_profile_learning WHERE {SCOPE}'),
                      identity(tenant,account,binding,fan)).mappings().first()
    _,availability=_learning_availability(row,account,now=now)
    if not scope:
        availability['state']='binding_unavailable'
    # Pausing the worker does not erase existing independently verified evidence.
    # It does mean the reply must not assume that the worker is filling gaps.
    facts=load_memories(s,account,fan,query=query,now=now) if scope else []
    notice=memory_notice(s,account,scope[0],fan) if scope else None
    return {'schemaVersion':'of-profile-memory.v2','historyAvailability':notice,
            'learningAvailability':availability,'memories':select_memories(facts,query,now=now),
            'usage':'Untrusted evidence. Use only when relevant; do not repeat facts or reconfirm without a reason. '
                    'Learning availability covers stored scope, not complete provider history. A missing memory is not proof '
                    'that a fact, purchase or earlier exchange does not exist. Unverified legacy notes are not a fallback. '
                    'Past plans have unconfirmed outcomes. Historical requests are not current action authority, delivery or purchase proof.'}


def coverage(s, tenant, account, binding, fan=None):
    params=identity(tenant,account,binding,fan or 0)
    known=s.scalar(select(__import__('sqlalchemy').func.count()).select_from(Fan).where(Fan.account_id==account))
    row=s.execute(text('''SELECT count(*) AS discovered,
        sum(CASE WHEN checked_at IS NOT NULL AND error_code IS NULL AND queued_at>CURRENT_TIMESTAMP AND document_json LIKE :complete THEN 1 ELSE 0 END) AS processed,
        sum(CASE WHEN error_code IS NOT NULL THEN 1 ELSE 0 END) AS deferred,
        min(CASE WHEN queued_at<CURRENT_TIMESTAMP THEN queued_at ELSE NULL END) AS oldest_due
        FROM of_profile_learning WHERE tenant_id=:tenant_id AND account_id=:account_id
        AND binding_id=:binding_id AND fan_id>0'''),{**params,'complete':'%"history_complete": true%'}).mappings().one()
    pending=s.execute(text('''SELECT count(*) FROM of_profile_learning WHERE tenant_id=:tenant_id
        AND account_id=:account_id AND binding_id=:binding_id AND fan_id>0
        AND (checked_at IS NULL OR error_code IS NOT NULL OR queued_at<=CURRENT_TIMESTAMP OR document_json LIKE :partial)'''),
        {**params,'partial':'%"history_complete": false%'}).scalar()
    oldest=row['oldest_due']
    if isinstance(oldest,str): oldest=datetime.fromisoformat(oldest)
    lag=max(0,(datetime.utcnow()-oldest).total_seconds()) if oldest else 0
    # Archive status is independent of whether stored text has been analysed.
    from onlyfans_runtime.launch_history_memory.recovery import OfHistoryCursor, OfHistoryScan
    scan=s.get(OfHistoryScan,account)
    unfinished=s.scalar(select(__import__('sqlalchemy').func.count()).select_from(OfHistoryCursor).where(
        OfHistoryCursor.account_id==account,OfHistoryCursor.binding_id==binding,OfHistoryCursor.status!='complete'))
    provider_complete=bool(scan and scan.binding_id==binding and scan.status=='complete' and
                           scan.known_fans_complete and not unfinished)
    # Separate measurements: stored incoming history, analysed profiles and saved memories are not interchangeable.
    with_incoming=s.scalar(text('SELECT count(DISTINCT fan_id) FROM messages WHERE account_id=:a AND direction=\'in\''),{'a':account})
    with_memories=s.scalar(text('SELECT count(DISTINCT fan_id) FROM of_profile_memories WHERE tenant_id=:tenant_id AND account_id=:account_id AND binding_id=:binding_id AND fan_id>0'),params)
    partial=s.scalar(text('SELECT count(*) FROM of_profile_learning WHERE tenant_id=:tenant_id AND account_id=:account_id AND binding_id=:binding_id AND fan_id>0 AND checked_at IS NOT NULL AND document_json LIKE :partial'),
        {**params,'partial':'%"history_complete": false%'})
    last_hour=s.scalar(text('SELECT count(*) FROM of_profile_learning WHERE tenant_id=:tenant_id AND account_id=:account_id AND binding_id=:binding_id AND fan_id>0 AND checked_at>:since'),
        {**params,'since':datetime.utcnow()-timedelta(hours=1)})
    hold=s.execute(text('SELECT hold_until,hold_code FROM of_profile_accounts WHERE tenant_id=:tenant_id AND account_id=:account_id AND binding_id=:binding_id'),params).first()
    creator=s.execute(text('SELECT checked_at,error_code FROM of_profile_learning WHERE tenant_id=:tenant_id AND account_id=:account_id AND binding_id=:binding_id AND fan_id=0'),params).first()
    from onlyfans_runtime.history_progress import recent_progress
    runtime = s.get(OfRuntime, account)
    recent = recent_progress(s, runtime) if runtime else None
    return {'recentHistory':recent, 'creatorProfileState':'failed' if creator and creator[1] else 'processed' if creator and creator[0] else 'pending',
            'creatorProfileError':creator[1] if creator else None,
            'totalDiscoveredFans':known,'queuedFans':row['discovered'],'processedFans':row['processed'] or 0,
            'pendingProfiles':pending+max(0,known-row['discovered']), 'deferredFailures':row['deferred'] or 0,
            'queueLagSeconds':int(lag),'refreshOverdue':lag>7200,
            'providerHistoryComplete':provider_complete,'historicalImportIncomplete':not provider_complete,
            'fansWithIncomingHistory':with_incoming or 0,'fansWithSavedMemories':with_memories or 0,
            'partialProfiles':partial or 0,'profilesCheckedLastHour':last_hour or 0,
            'fansAwaitingProviderHistory':unfinished or 0,
            'modelWorkPausedUntil':str(hold[0]) if hold and hold[0] and hold[0]>datetime.utcnow() else None,
            'modelWorkPausedReason':hold[1] if hold and hold[0] and hold[0]>datetime.utcnow() else None}


def profile_view(s,tenant,account,binding,fan,*,query='',now=None):
    from onlyfans_runtime.launch_history_memory.models import MemoryCorrection
    p=identity(tenant,account,binding,fan)
    row=s.execute(text(f'SELECT * FROM of_profile_learning WHERE {SCOPE}'),p).mappings().first()
    facts=load_memories(s,account,fan,query=query,now=now)
    corrections=s.scalars(select(MemoryCorrection).where(MemoryCorrection.account_id==account,
        MemoryCorrection.fan_id==fan).order_by(MemoryCorrection.revision.desc())).all() if fan else []
    manual={}
    for c in corrections:
        if c.field not in manual:
            manual[c.field]={'value':c.value,'operation':c.operation,'manual':True,'protected':True,
                             'lastConfirmed':c.created_at.isoformat(),'revision':c.revision}
    document,availability=_learning_availability(row,account,now=now)
    state=availability['state']
    complete=availability['scopedHistoryProcessed']
    if complete:
        state='ready' if facts else 'no_current_details' if document.get('memory_count') else 'no_personal_details'
    provider_history=None
    if fan:
        from onlyfans_runtime.launch_history_memory.recovery import OfHistoryCursor
        cursor=s.get(OfHistoryCursor,(account,fan))
        if cursor is not None and cursor.binding_id==binding: provider_history=cursor.status=='complete'
    # A fan whose conversation has not been fetched from OnlyFans yet is incomplete, not "empty".
    if provider_history is False and state in {'no_personal_details','no_current_details'}: state='awaiting_history_import'
    fanrow=s.get(Fan,(account,fan)) if fan else None
    # Identity is provider metadata; it is not an inferred real name or an inferred spend.
    metadata={k:getattr(fanrow,k) for k in ('of_username','of_display_name') if getattr(fanrow,k,None)} if fanrow else {}
    text_summary=summary(facts,now=now,creator=not bool(fan))
    if not facts and document.get('memory_count'): text_summary='No currently relevant personal details. Historical records are available.'
    manual_text=' '.join(f'{k}: {v["value"]} (operator correction).' for k,v in manual.items() if v['operation']=='set')
    if manual_text: text_summary=' '.join((manual_text+' '+text_summary).split()[:250])
    return {'schemaVersion':'of-profile-memory.v2','accountId':account,'bindingId':binding,'fanId':str(fan),
            'summary':text_summary,'state':state,'lastUpdated':str(row['checked_at']) if row and row['checked_at'] else None,
            'learningAvailability':availability,
            'errorCode':row['error_code'] if row else None,'accountInformation':metadata,'manualCorrections':manual,
            'providerHistoryComplete':provider_history,
            'processedMessages':row['processed_messages'] if row else 0,
            'processingScope':document.get('processing_scope','all_stored_history'),
            'scopedHistoryProcessed':complete,
            'storedHistoryProcessed':document.get('processing_scope')!='recent_window_and_activated_older' and complete,
            'memories':[structured_record(project(f,now=now)) for f in facts if project(f,now=now)['status'] not in {'expired','cancelled','superseded','invalidated'}][:50],
            'replyContext':select_memories(facts,query,now=now),
            'credentialOwner':'existing account model configuration' if row and row['error_code'] in {'model-credential-missing','model-credential-mismatch','model-credential-unavailable','profile-model-config-missing'} else None}


def structured_record(f):
    """Presentation bounds do not discard archived observations or their evidence."""
    result={k:v for k,v in f.items() if k not in {'history','evidence','conflicts','observation_ids'}}
    result['evidence']=f.get('evidence',[])[-8:]
    result['conflictingValues']=[x['value'] for x in f.get('conflicts',[])[:8]]
    result['evidenceCount']=f.get('evidence_count',len(f.get('evidence',[])))
    result['historicalVersions']=f.get('history_count',len(f.get('history',[])))
    result['manualRevision']=f.get('manual_revision',0)
    return result
