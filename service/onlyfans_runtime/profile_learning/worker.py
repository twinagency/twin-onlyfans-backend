"""Resumable, budgeted stored-history learning. No provider sends or reply controls."""
import asyncio
from datetime import datetime, timedelta
import hashlib
import json
import os
import signal
import threading
import uuid

from sqlalchemy import exists, select, text, tuple_, func, or_, and_
from sqlalchemy.orm import Session
from db.models import Fan, FanProfile, Message, OfCreatorBinding
from db.onlyfans_models import OfIngress, OfRuntime
from onlyfans_runtime.store import Store, Refused
from .evidence import digest, merge, project, summary, validate, FIELD_LOCKS
from .storage import SCOPE, identity, valid_observation

INTERVAL=timedelta(hours=2)
PAGE=40
CHUNK_CHARS=4000
CHUNK_OVERLAP=300
CALL_BYTES=45000
LEASE=timedelta(seconds=120)


def scoped(store,s,tenant,account,binding):
    runtime=store._runtime(s,tenant,account)
    if runtime.binding_id!=binding: raise Refused('profile-binding-changed')
    return runtime


def seed(store,tenant):
    """Repeated bounded discovery reaches every fan, independent of AI/session state."""
    with Session(store.engine) as s,s.begin():
        s.execute(text('SELECT pg_advisory_xact_lock(hashtext(:k))'),{'k':'profile-seed:'+tenant})
        bindings=s.scalars(select(OfCreatorBinding).join(OfRuntime,
            OfRuntime.binding_id==OfCreatorBinding.id).where(OfCreatorBinding.tenant_id==tenant,
            OfCreatorBinding.channel=='onlyfans',OfCreatorBinding.revoked_at.is_(None))).all()
        for b in bindings:
            scoped(store,s,tenant,b.account_id,b.id)
            p={'tenant':tenant,'account':b.account_id,'binding':b.id}
            s.execute(text('INSERT INTO of_profile_accounts(tenant_id,account_id,binding_id) VALUES (:tenant,:account,:binding) ON CONFLICT DO NOTHING'),p)
            s.execute(text('''INSERT INTO of_profile_learning(tenant_id,account_id,binding_id,fan_id)
                VALUES (:tenant,:account,:binding,0) ON CONFLICT DO NOTHING'''),p)
            s.execute(text('''INSERT INTO of_profile_learning(tenant_id,account_id,binding_id,fan_id)
                SELECT :tenant,f.account_id,:binding,f.fan_id FROM fans f WHERE f.account_id=:account
                AND NOT EXISTS (SELECT 1 FROM of_profile_learning p WHERE p.tenant_id=:tenant
                AND p.account_id=f.account_id AND p.binding_id=:binding AND p.fan_id=f.fan_id)
                ORDER BY f.fan_id LIMIT 500 ON CONFLICT DO NOTHING'''),p)
            s.execute(text('''INSERT INTO fan_profiles(account_id,fan_id,message_count_at_gen,
                total_spend_at_gen_cents,notes_applied_successfully)
                SELECT p.account_id,p.fan_id,0,0,false FROM of_profile_learning p
                WHERE p.tenant_id=:tenant AND p.account_id=:account AND p.binding_id=:binding
                AND p.fan_id>0 ON CONFLICT DO NOTHING'''),p)


class MessageChunks:
    """Lazy chunk view: a long source is never duplicated into a list of strings."""
    def __init__(self,body): self.body=body
    def __len__(self): return max(1,(len(self.body)+CHUNK_CHARS-CHUNK_OVERLAP-1)//(CHUNK_CHARS-CHUNK_OVERLAP))
    def __getitem__(self,index):
        if not 0<=index<len(self): raise IndexError(index)
        start=index*(CHUNK_CHARS-CHUNK_OVERLAP)
        return self.body[start:start+CHUNK_CHARS]


def chunks(body): return MessageChunks(body)


def fingerprint(m):
    return digest([m.body,m.is_unsent,m.direction,m.created_at.isoformat(),m.automation_kind])


def receipt_match(job):
    """SQL expression deliberately notices edits to previously fact-free messages."""
    return '''r.tenant_id=:tenant_id AND r.account_id=m.account_id AND r.binding_id=:binding_id
        AND r.fan_id=:fan_id AND r.source_fan=m.fan_id AND r.message_id=m.message_id
        AND r.body_md5=md5(m.body) AND r.source_at=m.created_at AND r.direction=m.direction
        AND r.unsent=m.is_unsent AND r.automation=coalesce(m.automation_kind,'')
        AND r.next_chunk=r.total_chunks'''


def source_page(store,job):
    p={k:job[k] for k in ('tenant_id','account_id','binding_id','fan_id')}
    with Session(store.engine) as s,s.begin():
        scoped(store,s,job['tenant_id'],job['account_id'],job['binding_id'])
        ids=s.execute(text(f'''SELECT m.fan_id,m.message_id FROM messages m
            WHERE m.account_id=:account_id AND (:fan_id=0 OR m.fan_id=:fan_id)
            AND (:fan_id<>0 OR m.direction='out') AND EXISTS (SELECT 1 FROM of_ingress i
                WHERE i.account_id=m.account_id AND i.binding_id=:binding_id AND i.fan_id=m.fan_id
                AND i.message_id=m.message_id AND i.status='applied')
            AND NOT EXISTS (SELECT 1 FROM of_profile_receipts r WHERE {receipt_match(job)})
            ORDER BY m.ingested_at,m.message_id,m.fan_id LIMIT {PAGE+1}'''),p).all()
        sources={}; progress=[]; size=0
        for fid,mid in ids[:PAGE]:
            m=s.get(Message,(job['account_id'],fid,mid))
            key=f'{fid}:{mid}'
            body_hash=hashlib.md5(m.body.encode(),usedforsecurity=False).hexdigest()
            old=s.execute(text(f'''SELECT * FROM of_profile_receipts WHERE {SCOPE}
                AND source_fan=:sf AND message_id=:mid'''),{**p,'sf':fid,'mid':mid}).mappings().first()
            same=bool(old and old['body_md5']==body_hash and (datetime.fromisoformat(old['source_at']) if isinstance(old['source_at'],str) else old['source_at'])==m.created_at
                and old['direction']==m.direction and bool(old['unsent'])==m.is_unsent
                and old['automation']==(m.automation_kind or ''))
            parts=chunks(m.body); start=old['next_chunk'] if same else 0
            # Withdrawal/blank/non-subject text is still receipted without model cost.
            skip=m.is_unsent or not m.body.strip() or m.direction!=('in' if job['fan_id'] else 'out')
            end=len(parts) if skip else min(start+1,len(parts))
            if not skip:
                fan=s.get(Fan,(job['account_id'],fid))
                source={'id':key,'message_source':key,'fan':fid,'message':mid,'text':parts[start],
                    'at':m.created_at.isoformat(),'direction':m.direction,'automation':m.automation_kind,
                    'digest':fingerprint(m),'timezone':getattr(fan,'timezone',None),'chunk':start,
                    'total_chunks':len(parts)}
                context=s.scalars(select(Message).where(Message.account_id==job['account_id'],Message.fan_id==fid,
                    Message.is_unsent.is_(False), tuple_(Message.created_at,Message.message_id)<
                    tuple_(m.created_at,m.message_id)).order_by(Message.created_at.desc(),Message.message_id.desc()).limit(2)).all()
                context_sources={}
                for c in reversed(context):
                    # Only current binding sources may disambiguate a short answer.
                    proof=s.scalar(select(OfIngress.event_key).where(OfIngress.account_id==job['account_id'],
                        OfIngress.binding_id==job['binding_id'],OfIngress.fan_id==fid,OfIngress.message_id==c.message_id,
                        OfIngress.status=='applied').limit(1))
                    if proof:
                        ck=f'{fid}:{c.message_id}'
                        context_sources['context:'+ck]={'id':'context:'+ck,'message_source':ck,'fan':fid,
                            'message':c.message_id,'text':c.body[-2000:],'at':c.created_at.isoformat(),
                            'direction':c.direction,'automation':c.automation_kind,'digest':fingerprint(c),'context_only':True}
                candidate={**context_sources,key:source}
                added=len(json.dumps(candidate,ensure_ascii=False).encode())
                if size+added>CALL_BYTES and sources: break
                sources.update(candidate);size+=added
            progress.append({'fan':fid,'mid':mid,'body_md5':body_hash,'at':m.created_at,
                'direction':m.direction,'unsent':m.is_unsent,'automation':m.automation_kind or '',
                'next':end,'total':len(parts),'digest':fingerprint(m),'reset':not same})
        # A message only counts as processed once every chunk has committed.
        count=sum(p['next']==p['total'] for p in progress)
        more=len(ids)>len(progress) or any(p['next']<p['total'] for p in progress)
        return sources,progress,more,count


def publish_creator(store,job,document):
    from onlyfans_runtime.launch_personality.workflow import PersonalityWorkflow
    wf=PersonalityWorkflow(store); current=wf.get(job['tenant_id'],job['account_id'])
    if current['bindingId']!=job['binding_id']: raise Refused('profile-binding-changed')
    if (current.get('document') or {}).get('review'): return
    with Session(store.engine) as s,s.begin():
        from db.models import AccountAiConfig
        from onlyfans_runtime.creator_facts import effective
        cfg=s.get(AccountAiConfig,job['account_id'])
        verified,source=effective(s,cfg) if cfg else ({},None)
        binding=s.get(OfCreatorBinding,job['binding_id'])
        enriched={**document,'onlyFansProfile':{'handle':binding.handle,'displayName':binding.creator_name},
                  'verifiedBackendFacts':verified if not source or source.get('state')=='current' else {},
                  'verifiedBackendState':source.get('state') if source else 'saved_backend_facts'}
    if (current.get('document') or {}).get('automaticProfile')==enriched: return
    def build(s,runtime,config,previous):
        old=json.loads(previous.document_json) if previous else {'draft':{'creatorFacts':[],
            'writingStyle':[],'operationalInstructions':[],'evidence':[],'coverage':{}},
            'review':{},'source':'automatic_onlyfans_history'}
        if old.get('review'): raise Refused('profile-review-in-progress')
        old['automaticProfile']=enriched
        old['draft']['suggestedPersona']={'text':document.get('summary',''),'attribution':'unknown',
            'evidenceIds':[],'selected':False,'requiresReview':True,'basis':'observed_account_claims',
            'warning':'Outgoing messages may be written by agency staff or previous automation. Verified backend facts remain separate.'}
        return old,'draft'
    wf._change(job['tenant_id'],job['account_id'],current['revision'],digest(['automatic-profile',enriched]),
        'worker-import:'+digest(['profile-learning',job['binding_id']]),
        {'action':'automatic_profile','documentHash':digest(document)},build,expected_binding_id=job['binding_id'])


def claim_job(store,tenant,stop):
    with Session(store.engine) as s,s.begin():
        # A row lock claims a single job, not the whole tenant. Leases recover crashes.
        suffix=' FOR UPDATE OF p SKIP LOCKED' if store.engine.dialect.name=='postgresql' else ''
        job=s.execute(text('''SELECT p.* FROM of_profile_learning p
            JOIN of_creator_bindings b ON b.id=p.binding_id AND b.account_id=p.account_id
            JOIN of_profile_accounts scheduler ON scheduler.tenant_id=p.tenant_id AND scheduler.account_id=p.account_id AND scheduler.binding_id=p.binding_id
            JOIN of_runtime runtime ON runtime.account_id=p.account_id AND runtime.binding_id=p.binding_id
            LEFT JOIN fans f ON f.account_id=p.account_id AND f.fan_id=p.fan_id
            WHERE p.tenant_id=:tenant AND b.tenant_id=:tenant AND b.revoked_at IS NULL
            AND b.channel='onlyfans' AND p.next_at<=CURRENT_TIMESTAMP
            AND (p.lease_until IS NULL OR p.lease_until<CURRENT_TIMESTAMP)
            ORDER BY scheduler.last_claimed_at,
            CASE WHEN p.next_at<:starved THEN 0 WHEN f.last_message_received_at>:active THEN 1 ELSE 2 END,
            p.next_at,p.account_id,p.fan_id LIMIT 1'''+suffix),
            {'tenant':tenant,'starved':datetime.utcnow()-timedelta(hours=6),'active':datetime.utcnow()-timedelta(days=1)}).mappings().first()
        if not job or stop.is_set(): return None
        job=dict(job);job['lease_token']=str(uuid.uuid4())
        s.execute(text('UPDATE of_profile_accounts SET last_claimed_at=CURRENT_TIMESTAMP WHERE tenant_id=:tenant_id AND account_id=:account_id AND binding_id=:binding_id'),job)
        s.execute(text(f'UPDATE of_profile_learning SET lease_token=:lease_token,lease_until=:until WHERE {SCOPE}'),
            {**job,'until':datetime.utcnow()+LEASE})
        return job


def _persist(store,job,sources,progress,additions,more,count,call_id):
    p={k:job[k] for k in ('tenant_id','account_id','binding_id','fan_id')}
    with Session(store.engine) as s,s.begin():
        scoped(store,s,job['tenant_id'],job['account_id'],job['binding_id'])
        lock=' FOR UPDATE' if store.engine.dialect.name=='postgresql' else ''
        current=s.execute(text(f'SELECT lease_token,lease_until,document_json FROM of_profile_learning WHERE {SCOPE}'+lock),p).mappings().one()
        until=current['lease_until']
        if isinstance(until,str): until=datetime.fromisoformat(until)
        if current['lease_token']!=job['lease_token'] or until<datetime.utcnow(): raise Refused('profile-lease-lost')
        document=json.loads(current['document_json'])
        for part in progress:
            m=s.get(Message,(job['account_id'],part['fan'],part['mid']))
            if m is None or fingerprint(m)!=part['digest']: raise Refused('profile-source-changed')
            if part['reset']:
                s.execute(text(f'DELETE FROM of_profile_observations WHERE {SCOPE} AND source_fan=:sf AND message_id=:mid'),
                          {**p,'sf':part['fan'],'mid':part['mid']})
            s.execute(text('''INSERT INTO of_profile_receipts(tenant_id,account_id,binding_id,fan_id,source_fan,
                message_id,body_md5,source_at,direction,unsent,automation,next_chunk,total_chunks)
                VALUES (:tenant_id,:account_id,:binding_id,:fan_id,:sf,:mid,:hash,:at,:direction,:unsent,:auto,:next,:total)
                ON CONFLICT(tenant_id,account_id,binding_id,fan_id,source_fan,message_id) DO UPDATE SET
                body_md5=excluded.body_md5,source_at=excluded.source_at,direction=excluded.direction,
                unsent=excluded.unsent,automation=excluded.automation,next_chunk=excluded.next_chunk,
                total_chunks=excluded.total_chunks,processed_at=CURRENT_TIMESTAMP'''),
                {**p,'sf':part['fan'],'mid':part['mid'],'hash':part['body_md5'],'at':part['at'].strftime('%Y-%m-%d %H:%M:%S.%f'),
                 'direction':part['direction'],'unsent':part['unsent'],'auto':part['automation'],'next':part['next'],'total':part['total']})
        for f in additions:
            if not valid_observation(s,job['account_id'],job['binding_id'],f): raise Refused('profile-source-changed')
            f.update(tenant_id=job['tenant_id'],account_id=job['account_id'],binding_id=job['binding_id'],fan_id=job['fan_id'])
            oid=digest([f['source'],f['digest'],f['field'],f['value'],f.get('key'),f.get('operation')])
            sf,mid=map(int,f['source'].split(':'))
            s.execute(text('''INSERT INTO of_profile_observations(tenant_id,account_id,binding_id,fan_id,
                observation_id,source_fan,message_id,document_json)
                VALUES(:tenant_id,:account_id,:binding_id,:fan_id,:oid,:sf,:mid,:doc) ON CONFLICT DO NOTHING'''),
                {**p,'oid':oid,'sf':sf,'mid':mid,'doc':json.dumps(f,ensure_ascii=False)})
        observations=[]
        for row in s.execute(text(f'SELECT observation_id,document_json FROM of_profile_observations WHERE {SCOPE}'),p):
            f=json.loads(row[1])
            if valid_observation(s,job['account_id'],job['binding_id'],f): observations.append((row[0],f))
            else:
                s.execute(text(f'DELETE FROM of_profile_observations WHERE {SCOPE} AND observation_id=:oid'),{**p,'oid':row[0]})
                # A changed preceding question can change a short answer's meaning.
                sf,mid=map(int,f['source'].split(':'))
                s.execute(text(f'DELETE FROM of_profile_receipts WHERE {SCOPE} AND source_fan=:sf AND message_id=:mid'),{**p,'sf':sf,'mid':mid})
        facts=merge([],[f for _,f in observations])
        from onlyfans_runtime.launch_history_memory.memory import locked_fields
        locks=locked_fields(s,job['account_id'],job['fan_id']) if job['fan_id'] else set()
        facts=[f for f in facts if not locks & FIELD_LOCKS.get(f['field'],set())]
        s.execute(text(f'DELETE FROM of_profile_memories WHERE {SCOPE}'),p)
        for f in facts:
            f['evidence_count']=len(f.get('evidence',[]));f['history_count']=len(f.get('history',[]))
            # Keep the materialized projection small. Every observation remains in
            # the separate archival table; no fact is evicted by chatter.
            f['evidence']=f.get('evidence',[])[-8:]
            f['history']=[]
            f['conflicts']=[{k:v for k,v in c.items() if k not in {'history','conflicts'}} for c in f.get('conflicts',[])[:8]]
            f=project(f)
            s.execute(text('''INSERT INTO of_profile_memories(tenant_id,account_id,binding_id,fan_id,
                memory_key,field,status,kind,document_json) VALUES(:tenant_id,:account_id,:binding_id,:fan_id,
                :key,:field,:status,:kind,:doc)'''),{**p,'key':f['memory_key'],'field':f['field'],
                'status':f['status'],'kind':f['kind'],'doc':json.dumps(f,ensure_ascii=False)})
        remaining=s.execute(text(f'''SELECT 1 FROM messages m WHERE m.account_id=:account_id
            AND (:fan_id=0 OR m.fan_id=:fan_id) AND (:fan_id<>0 OR m.direction='out')
            AND EXISTS(SELECT 1 FROM of_ingress i WHERE i.account_id=m.account_id AND i.binding_id=:binding_id
                AND i.fan_id=m.fan_id AND i.message_id=m.message_id AND i.status='applied')
            AND NOT EXISTS(SELECT 1 FROM of_profile_receipts r WHERE {receipt_match(job)}) LIMIT 1'''),p).first()
        more=remaining is not None
        document.pop('facts',None)  # V1's arbitrary six-per-field store is replaced, not carried into replies.
        document.update(schema_version='of-profile-memory.v2',summary=summary(facts,creator=not bool(job['fan_id'])),
            memory_count=len(facts),history_complete=not more,summary_locked=bool(locks & {'short_bio','bullet_points'}),
            coverage='Stored current-binding messages only; provider import is tracked separately.')
        # V2 owns its summary separately. It must not overwrite legacy/manual FanProfile notes.
        due=datetime.utcnow()+(timedelta(seconds=1) if more else INTERVAL)
        s.execute(text(f'''UPDATE of_profile_learning SET document_json=:doc,processed_messages=
            (SELECT count(*) FROM of_profile_receipts r WHERE r.tenant_id=:tenant_id AND r.account_id=:account_id
            AND r.binding_id=:binding_id AND r.fan_id=:fan_id AND r.next_chunk=r.total_chunks),
            next_at=:due,queued_at=CASE WHEN :more THEN queued_at ELSE :due END,
            last_completed_at=CASE WHEN :more THEN last_completed_at ELSE CURRENT_TIMESTAMP END,
            checked_at=CURRENT_TIMESTAMP,error_code=NULL,
            attempts=0,maintenance_at=CURRENT_TIMESTAMP WHERE {SCOPE}'''),{**p,'doc':json.dumps(document,ensure_ascii=False),'due':due,'more':more})
        return document


async def step(store,tenant,stop,*,extractor=None):
    if extractor is None:
        from .model import extract
        extractor=extract
    job=claim_job(store,tenant,stop)
    if not job: return False
    p={k:job[k] for k in ('tenant_id','account_id','binding_id','fan_id')}
    try:
        sources,progress,more,count=source_page(store,job)
        additions=[];call_id=None
        if any(not v.get('context_only') for v in sources.values()):
            result,call_id=await extractor(store,tenant,job['account_id'],job['binding_id'],job['fan_id'],sources,creator=not bool(job['fan_id']))
            additions=validate(result,sources,creator=not bool(job['fan_id']))
        document=_persist(store,job,sources,progress,additions,more,count,call_id)
        if not job['fan_id']: publish_creator(store,job,document)
        with store.engine.begin() as c:
            c.execute(text(f'UPDATE of_profile_learning SET lease_token=NULL,lease_until=NULL WHERE {SCOPE} AND lease_token=:token'),{**p,'token':job['lease_token']})
        return True
    except Exception as exc:
        from native_pilot.budget import BudgetRefused
        from llm_client import LLMCapExceeded
        allowed={'model-credential-missing','model-credential-mismatch','model-credential-unavailable','profile-binding-changed',
                 'profile-model-usage-unavailable','profile-model-config-missing','profile-source-changed','profile-review-in-progress'}
        code=str(exc) if isinstance(exc,Refused) and str(exc) in allowed else 'profile-budget-exhausted' if isinstance(exc,(BudgetRefused,LLMCapExceeded)) else 'profile-analysis-deferred'
        attempts=int(job.get('attempts',0))+1
        with Session(store.engine) as s,s.begin():
            s.execute(text(f'''UPDATE of_profile_learning SET error_code=:error,next_at=:due,
                lease_token=NULL,lease_until=NULL,attempts=:attempts WHERE {SCOPE} AND lease_token=:token'''),
                {**p,'error':code,'due':datetime.utcnow()+timedelta(minutes=min(120,5*2**min(attempts,5))),
                 'attempts':attempts,'token':job['lease_token']})
        return True


def daily_maintenance(store,tenant):
    """Re-evaluation is also performed on every read. Daily sweep rebuilds cold summaries."""
    with Session(store.engine) as s,s.begin():
        s.execute(text('''UPDATE of_profile_learning SET next_at=CURRENT_TIMESTAMP WHERE tenant_id=:tenant
            AND next_at>CURRENT_TIMESTAMP AND (maintenance_at IS NULL OR maintenance_at<:yesterday)'''),
            {'tenant':tenant,'yesterday':datetime.utcnow()-timedelta(days=1)})


def main():
    tenant=os.environ.get('ONLYFANS_WORKER_TENANT','')
    if os.environ.get('ONLYFANS_PROCESS_ROLE')!='worker' or not tenant or tenant=='deployment':
        raise SystemExit('Explicit worker role and tenant required')
    concurrency=int(os.environ.get('ONLYFANS_PROFILE_CONCURRENCY','2'))
    if not 1<=concurrency<=4: raise SystemExit('Profile concurrency must be 1–4')
    stop=threading.Event()
    for sig in (signal.SIGINT,signal.SIGTERM): signal.signal(sig,lambda *_:stop.set())
    store=Store(os.environ['DATABASE_URL'])
    async def run():
        last_maintenance=datetime.min
        try:
            while not stop.is_set():
                try:
                    seed(store,tenant)
                    if datetime.utcnow()-last_maintenance>timedelta(days=1):
                        daily_maintenance(store,tenant);last_maintenance=datetime.utcnow()
                    worked=any(await asyncio.gather(*(step(store,tenant,stop) for _ in range(concurrency))))
                except Exception:
                    print(json.dumps({'component':'profile-learning','state':'unavailable'}),flush=True);worked=False
                await asyncio.sleep(1 if worked else 30)
        finally:
            from db.engine import engine
            await engine.dispose()
    try: asyncio.run(run())
    finally: store.close()


if __name__=='__main__': main()
