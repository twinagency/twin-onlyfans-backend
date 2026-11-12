"""Pilot-only liveness gate around the reviewed worker, never a send grant."""
import dataclasses,json,os,time
from pathlib import Path
from incident_state import unresolved
from model_transport.pilot_supervised import SupervisedPilot,build_supervised_pilot
from onlyfans_runtime.store import Store,Refused
import plan_c_worker
GATE=Path('/run/guard/budget-state.json')
INCIDENT=Path('/run/guard/outage-open.json')
BOOT=Path('/proc/sys/kernel/random/boot_id')
def permitted(revision,*,account=None,binding=None,epoch=None):
 try:
  if unresolved(INCIDENT):return False
  state=json.loads(GATE.read_text());stamp=state['last_ok_monotonic'];clock=time.monotonic()
  if account is not None and (state.get('account_id')!=account or state.get('binding_id')!=binding
      or type(state.get('connection_epoch')) is not int or state.get('connection_epoch')!=epoch):return False
  return bool(state.get('scope_status')=='active' and type(state.get('scope_revision')) is int
              and state['scope_revision']==revision and state.get('boot_id')==BOOT.read_text().strip()
              and type(stamp) in (int,float) and 0<=clock-stamp<=45)
 except Exception:return False
def fence_client(client,revision,**context):
 # The existing durable request guard and idempotency logic remain untouched.
 # A last-moment refusal inside _request becomes unknown, never a blind retry.
 original_send,original_http=client.send,client._http
 def send(*args,**kwargs):
  if not permitted(revision,**context):raise Refused('supervisor liveness unavailable before send')
  return original_send(*args,**kwargs)
 def http(**kwargs):
  if kwargs.get('method')=='POST' and not permitted(revision):raise Refused('supervisor liveness unavailable before wire')
  return original_http(**kwargs)
 client.send=send;client._http=http
class GatedPilot(SupervisedPilot):
 def guard_context(self):
  from sqlalchemy.orm import Session
  with Session(self.worker.engine.store.engine) as session:
   runtime=self.worker.engine.store._runtime(session,self.tenant,self.account,lock=False)
   return {'account':self.account,'binding':runtime.binding_id,'epoch':self.worker.transport.client.epoch}
 def tick(self,*,limit=5):
  if not permitted(self.scope_revision,**self.guard_context()):
   self.validate()
   return {'outcome':'supervisor_liveness_hold'}
  return super().tick(limit=limit)
def main():
 if os.environ.get('TWIN_PLAN_C_SUPERVISED')!='1' or os.environ.get('ONLYFANS_PROCESS_ROLE')!='worker' or not os.environ.get('DATABASE_URL'):raise Refused('explicit pilot worker required')
 import uvicorn
 store=Store(os.environ['DATABASE_URL'])
 try:
  base=build_supervised_pilot(store)
  if type(base) is not SupervisedPilot:raise Refused('active pilot required for sender')
  pilot=GatedPilot(**{f.name:getattr(base,f.name) for f in dataclasses.fields(SupervisedPilot)})
  fence_client(pilot.worker.transport.client,pilot.scope_revision,**pilot.guard_context())
  port=os.environ.get('TWIN_PLAN_C_LISTEN_PORT','55464')
  if not port.isascii() or not port.isdecimal() or not 1024<=int(port)<=65535:raise Refused('invalid sender loopback port')
  uvicorn.run(plan_c_worker.create_app(pilot),host='127.0.0.1',port=int(port),access_log=False)
 finally:store.close()
if __name__=='__main__':main()
