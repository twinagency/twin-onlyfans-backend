"""Bounded repair of the user-reported conversation using existing import fences."""
import os,json,time,hashlib
from datetime import timezone
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from db.models import Message
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_delivery_models import OfDeliveryPolicy
from onlyfans_runtime.store import Store,utcnow,MAX_PENDING
from onlyfans_runtime.connections import _open
from onlyfans_runtime.provider_connector import NativeProviderConnector,session_from_capture
from onlyfans_runtime.launch_history_memory.recovery import HistoryRecovery,OfHistoryCursor,ArchiveClaim,LIVE_RESERVE

ACCOUNT='of-312988353';FAN=321347934
tenant=os.environ['ONLYFANS_WORKER_TENANT'];store=Store(os.environ['DATABASE_URL'])
with Session(store.engine) as s,s.begin():
 runtime=store._runtime(s,tenant,ACCOUNT);row=s.get(OfProviderConnection,ACCOUNT)
 store._read_connection(s,runtime,row.revision)
 clear=_open(row,tenant);revision=row.revision;expires=row.expires_at.replace(tzinfo=timezone.utc)
 policy=s.get(OfDeliveryPolicy,ACCOUNT);policy_revision=policy.revision if policy else 1
reader=NativeProviderConnector(session_from_capture(account_id=ACCOUNT,owner_id=clear['ownerId'],policy_revision=policy_revision,capture=clear['capture'],x_hash=clear['xHash'],proxy_url=clear['proxyUrl'],expires_at=expires))
reader.connection_revision=revision

class ReportedFanRecovery(HistoryRecovery):
 def claim_scan(self,*args,**kwargs):return None
 def claim(self,tenant_id,account_id,*,connection_revision=None):
  assert account_id==ACCOUNT
  now=utcnow()
  with Session(self.store.engine) as s,s.begin():
   runtime=self._runtime(s,tenant_id,account_id,connection_revision)
   if runtime.pending_events>=MAX_PENDING-LIVE_RESERVE:return None
   if runtime.enabled and self._live_work(s,account_id,FAN):return None
   row=s.scalar(select(OfHistoryCursor).where(OfHistoryCursor.account_id==account_id,OfHistoryCursor.fan_id==FAN,self._eligible(OfHistoryCursor,now)))
   if row is None:return None
   self._binding(row,runtime);self._lease(row,now)
   return ArchiveClaim(account_id,runtime.binding_id,FAN,row.generation,row.lease_token,row.before_id,connection_revision)

recovery=ReportedFanRecovery(store)
from onlyfans_runtime.history_repair import inspect,repair
snapshot=inspect(store,tenant,ACCOUNT,FAN)
if snapshot['records'] and all(row['verified'] for row in snapshot['records']):
 key=hashlib.sha256(('history-capacity-20261101:'+snapshot['fingerprint']).encode()).hexdigest()
 result=repair(store,tenant,ACCOUNT,FAN,command_key=key,
     actor='worker-import:'+hashlib.sha256(b'history-capacity-20261101').hexdigest(),
     reason='User-requested history repair: verified HTML representation equivalence; retain canonical message and raw observation',
     expected_fingerprint=snapshot['fingerprint'])
 print(json.dumps({'representationRepair':result['repaired'],'retained':result['retained']}),flush=True)
start=time.monotonic();steps=0
try:
 while time.monotonic()-start<100 and steps<12:
  try:steps+=recovery.step(tenant,ACCOUNT,reader,connection_revision=revision)
  except Exception as e:print(json.dumps({'errorType':type(e).__name__}),flush=True);break
  with Session(store.engine) as s:
   row=s.get(OfHistoryCursor,(ACCOUNT,FAN))
   count,oldest,newest=s.execute(select(func.count(),func.min(Message.created_at),func.max(Message.created_at)).where(Message.account_id==ACCOUNT,Message.fan_id==FAN)).one()
   print(json.dumps({'account':ACCOUNT,'fan':FAN,'status':row.status,'pages':row.pages_completed,'messages':count,'oldest':str(oldest),'newest':str(newest),'lastError':row.last_error}),flush=True)
   if row.status=='complete':break
  time.sleep(2)
finally:store.close()
