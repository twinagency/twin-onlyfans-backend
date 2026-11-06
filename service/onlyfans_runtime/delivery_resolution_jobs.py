"""Audited evidence requests. Provider reads run in one-account worker children."""
import asyncio,hashlib,json,os,subprocess,sys
from datetime import timedelta
from sqlalchemy import select,text
from sqlalchemy.orm import Session,aliased
from db.models import OfConversationState,OfCreatorBinding
from db.onlyfans_models import OfRuntime
from db.onlyfans_delivery_models import OfTextIntent
from .launch_operations_models import OfLaunchCommand,OfLaunchWorkerHealth
from .launch_operations import digest,command_identity,_prior,_record
from .store import Store,Refused,utcnow

REQUEST='delivery_resolution_request'
RESULT='delivery_resolution_result'
_child=None

def row_ready(runtime,row):
    if not row or not runtime or row.binding_id!=runtime.binding_id or not timedelta(0)<=utcnow()-row.observed_at<=timedelta(seconds=180):return False
    try:return json.loads(row.capabilities_json).get('delivery_resolution') is True
    except (ValueError,TypeError,AttributeError):return False

def available_for(store,tenant,account):
    from .delivery_resolution import operational
    if not operational():return False
    with Session(store.engine) as s,s.begin():
        runtime=store._runtime(s,tenant,account)
        return row_ready(runtime,s.get(OfLaunchWorkerHealth,account))

async def enqueue(store,tenant,account,fan,*,attempt,provider_message_id,expected_revision,command_key,actor):
    fingerprint=digest([fan,attempt,provider_message_id,expected_revision,actor])
    command_identity(command_key,actor,'Dashboard delivery evidence request',fingerprint)
    with Session(store.engine) as s,s.begin():
        runtime=store._runtime(s,tenant,account)
        prior=_prior(s,runtime,command_key,REQUEST,fingerprint)
        done=s.get(OfLaunchCommand,(account,digest([RESULT,command_key])))
        if done and json.loads(done.result_json).get('state')!='resolved':raise Refused('unresolved_delivery')
        if prior is not None:return prior
        from .delivery_resolution import operational
        if not operational() or not row_ready(runtime,s.get(OfLaunchWorkerHealth,account)):raise Refused('delivery resolution unavailable')
        state=s.get(OfConversationState,(account,fan))
        if not state or state.revision!=expected_revision or state.unresolved_attempt_id!=attempt:raise Refused('control revision conflict')
        intent=s.scalar(select(OfTextIntent).where(OfTextIntent.account_id==account,OfTextIntent.fan_id==fan,OfTextIntent.attempt_id==attempt))
        if intent is not None and (intent.binding_id!=runtime.binding_id or intent.status not in ('dispatching','unknown')):raise Refused('unresolved_delivery')
        if intent is None:
            import re
            match=re.fullmatch(r'fastt-pilot-([1-9][0-9]*)',attempt)
            if not match:raise Refused('unresolved_delivery')
            raw=s.scalar(text('SELECT r.evidence_json FROM fastt_runtime.native_pilot_receipts r JOIN fastt_runtime.native_pilots p ON p.pilot_id=r.pilot_id WHERE p.account_id=:a AND p.fan_id=:f AND r.attempt_id=:i'),{'a':account,'f':fan,'i':int(match[1])})
            try:frozen=json.loads(raw or '{}').get('dispatch_intent')
            except (ValueError,TypeError):frozen=None
            if not frozen or frozen.get('binding_id')!=runtime.binding_id:raise Refused('unresolved_delivery')
        request={'tenant':tenant,'fan':fan,'attempt':attempt,'provider_message_id':provider_message_id,'expected_revision':expected_revision,'actor':actor,'command_key':command_key}
        return _record(s,runtime,command_key,REQUEST,fingerprint,actor,'Dashboard delivery evidence request',request,{'recorded':True,'state':'queued'},utcnow())

def poll(store,tenant):
    global _child
    from .delivery_resolution import operational
    if os.environ.get('ONLYFANS_PROCESS_ROLE')!='worker' or not operational():return
    if _child is not None:
        if _child.poll() is None:return
        _child=None
    result=aliased(OfLaunchCommand)
    with Session(store.engine) as s:
        row=s.scalar(select(OfLaunchCommand).join(OfRuntime,OfRuntime.account_id==OfLaunchCommand.account_id).join(OfCreatorBinding,OfCreatorBinding.id==OfRuntime.binding_id).where(
            OfCreatorBinding.tenant_id==tenant,OfCreatorBinding.revoked_at.is_(None),OfLaunchCommand.binding_id==OfRuntime.binding_id,OfLaunchCommand.kind==REQUEST,
            ~select(result.command_key).where(result.account_id==OfLaunchCommand.account_id,result.kind==RESULT,result.request_hash==OfLaunchCommand.command_key).exists()
        ).order_by(OfLaunchCommand.created_at).limit(1))
        if row is None:return
        account,key=row.account_id,row.command_key
    _child=subprocess.Popen([sys.executable,'-m','onlyfans_runtime.delivery_resolution_jobs','--account',account,'--key',key],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

def process_request(store,account,key):
    from .delivery_resolution import operational,perform
    if os.environ.get('ONLYFANS_PROCESS_ROLE')!='worker' or not operational():raise Refused('worker resolution unavailable')
    lock=int(hashlib.sha256(('crm-resolution:'+account+':'+key).encode()).hexdigest()[:15],16)
    with store.engine.connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
        if not connection.scalar(text('SELECT pg_try_advisory_lock(:key)'),{'key':lock}):return
        try:
            with Session(store.engine) as s:
                row=s.get(OfLaunchCommand,(account,key))
                if row is None or row.kind!=REQUEST:raise Refused('resolution request unavailable')
                request=json.loads(row.before_json);tenant=request.pop('tenant');binding=row.binding_id;created=row.created_at
                result_key=digest([RESULT,key])
                if s.get(OfLaunchCommand,(account,result_key)) is not None:return
            state='refused';reason='request_expired'
            if utcnow()-created<=timedelta(minutes=5):
                try:
                    request['command_key']=digest(['delivery_resolution_evidence',key])
                    asyncio.run(perform(store,tenant,account,**request));state='resolved';reason='evidence_reconciled'
                except Refused:reason='evidence_or_scope_refused'
                except (TimeoutError,asyncio.TimeoutError):reason='evidence_check_timeout'
                except Exception:reason='evidence_check_failed'
            with Session(store.engine) as s,s.begin():
                runtime=store._runtime(s,tenant,account)
                if runtime.binding_id!=binding:raise Refused('scope unavailable')
                if s.get(OfLaunchCommand,(account,result_key)) is None:
                    _record(s,runtime,result_key,RESULT,key,request['actor'],'Worker provider evidence decision',{'requestKey':key},{'recorded':True,'state':state,'reason':reason},utcnow())
        finally:connection.execute(text('SELECT pg_advisory_unlock(:key)'),{'key':lock})

if __name__=='__main__':
    import argparse,re
    parser=argparse.ArgumentParser();parser.add_argument('--account',required=True);parser.add_argument('--key',required=True);args=parser.parse_args()
    if not re.fullmatch(r'of-[1-9][0-9]*',args.account) or not re.fullmatch(r'[a-f0-9]{64}',args.key):raise SystemExit(2)
    store=Store(os.environ['DATABASE_URL'])
    try:process_request(store,args.account,args.key)
    finally:store.close()
