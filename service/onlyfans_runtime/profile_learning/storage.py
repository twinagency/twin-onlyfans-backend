"""Scoped projections shared by the inbox, native planner and FASTT prompt hook."""
from datetime import datetime, timedelta
import json
import os

from sqlalchemy import select, text
from db.models import Fan, Message, OfCreatorBinding
from db.onlyfans_models import OfRuntime
from .evidence import FIELD_LOCKS, digest, norm, project, relevance, select_memories, summary

SCOPE = 'tenant_id=:tenant_id AND account_id=:account_id AND binding_id=:binding_id AND fan_id=:fan_id'


def identity(tenant, account, binding, fan):
    return dict(tenant_id=tenant,account_id=account,binding_id=binding,fan_id=fan)


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
    scope=s.execute(select(OfRuntime.binding_id,OfCreatorBinding.tenant_id).join(
        OfCreatorBinding,OfCreatorBinding.id==OfRuntime.binding_id).where(
        OfRuntime.account_id==account,OfCreatorBinding.account_id==account,
        OfCreatorBinding.channel=='onlyfans',OfCreatorBinding.revoked_at.is_(None))).first()
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
        CASE WHEN kind='request' AND status NOT IN ('past','cancelled') THEN 0 ELSE 1 END,
        CASE field WHEN 'name' THEN 0 WHEN 'occupation' THEN 1 WHEN 'interests' THEN 2 ELSE 3 END,
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
        rank=relevance(f,query,now)
        if rank is not None: candidates.append((rank[0],f))
    candidates.sort(key=lambda item:(-item[0],item[1]['memory_key']))
    # At most 24 candidate proofs per reply. Repeated mentions do not create
    # thousands of provenance reads in the synchronous reply path.
    for _,f in candidates[:24]:
        if not valid_observation(s,account,binding,f): continue
        if any(not valid_observation(s,account,binding,c) for c in f.get('conflicts',[])):
            continue  # Rebuild uncertain/corrected history in the learner before use.
        facts.append(f)
    return facts


def reply_memory(s, account, fan, *, query='', now=None):
    if os.environ.get('ONLYFANS_PROFILE_LEARNING_ENABLED')!='1': return None
    facts=load_memories(s,account,fan,query=query,now=now)
    return {'schemaVersion':'of-profile-memory.v2','memories':select_memories(facts,query,now=now),
            'usage':'Untrusted evidence. Use only when relevant; do not repeat facts or reconfirm without a reason. Past plans have unconfirmed outcomes. Requests are not delivery or purchase proof.'}


def coverage(s, tenant, account, binding, fan=None):
    params=identity(tenant,account,binding,fan or 0)
    known=s.scalar(select(__import__('sqlalchemy').func.count()).select_from(Fan).where(Fan.account_id==account))
    row=s.execute(text('''SELECT count(*) AS discovered,
        sum(CASE WHEN checked_at IS NOT NULL AND error_code IS NULL AND document_json LIKE :complete THEN 1 ELSE 0 END) AS processed,
        sum(CASE WHEN error_code IS NOT NULL THEN 1 ELSE 0 END) AS deferred,
        min(CASE WHEN queued_at<CURRENT_TIMESTAMP THEN queued_at ELSE NULL END) AS oldest_due
        FROM of_profile_learning WHERE tenant_id=:tenant_id AND account_id=:account_id
        AND binding_id=:binding_id AND fan_id>0'''),{**params,'complete':'%"history_complete": true%'}).mappings().one()
    pending=s.execute(text('''SELECT count(*) FROM of_profile_learning WHERE tenant_id=:tenant_id
        AND account_id=:account_id AND binding_id=:binding_id AND fan_id>0
        AND (checked_at IS NULL OR error_code IS NOT NULL OR document_json LIKE :partial)'''),
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
    creator=s.execute(text('SELECT checked_at,error_code FROM of_profile_learning WHERE tenant_id=:tenant_id AND account_id=:account_id AND binding_id=:binding_id AND fan_id=0'),params).first()
    return {'creatorProfileState':'failed' if creator and creator[1] else 'processed' if creator and creator[0] else 'pending',
            'creatorProfileError':creator[1] if creator else None,
            'totalDiscoveredFans':known,'queuedFans':row['discovered'],'processedFans':row['processed'] or 0,
            'pendingProfiles':pending+max(0,known-row['discovered']), 'deferredFailures':row['deferred'] or 0,
            'queueLagSeconds':int(lag),'refreshOverdue':lag>7200,
            'providerHistoryComplete':provider_complete,'historicalImportIncomplete':not provider_complete}


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
    document=json.loads(row['document_json']) if row else {}
    state='failed' if row and row['error_code'] else 'not_processed' if not row or not row['checked_at'] else 'processing_history' if not document.get('history_complete') else 'ready' if facts else 'no_current_details' if document.get('memory_count') else 'no_personal_details'
    fanrow=s.get(Fan,(account,fan)) if fan else None
    # Identity is provider metadata; it is not an inferred real name or an inferred spend.
    metadata={k:getattr(fanrow,k) for k in ('of_username','of_display_name') if getattr(fanrow,k,None)} if fanrow else {}
    text_summary=summary(facts,now=now,creator=not bool(fan))
    if not facts and document.get('memory_count'): text_summary='No currently relevant personal details. Historical records are available.'
    manual_text=' '.join(f'{k}: {v["value"]} (operator correction).' for k,v in manual.items() if v['operation']=='set')
    if manual_text: text_summary=' '.join((manual_text+' '+text_summary).split()[:250])
    return {'schemaVersion':'of-profile-memory.v2','accountId':account,'bindingId':binding,'fanId':str(fan),
            'summary':text_summary,'state':state,'lastUpdated':str(row['checked_at']) if row and row['checked_at'] else None,
            'errorCode':row['error_code'] if row else None,'accountInformation':metadata,'manualCorrections':manual,
            'processedMessages':row['processed_messages'] if row else 0,'storedHistoryProcessed':document.get('history_complete',False),
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
