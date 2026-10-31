"""Same arrival trace and socket fakes through the deployed native Service; disposable PG only."""
import argparse,importlib.util,inspect,json,os,random,sys
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
sys.path[:0]=['/app/service','/app/service/tests','/app/service/native_pilot/tools']
import load_test as h
ON=os.environ.get('PACING_TEST_ON')=='1'
original_bootstrap=h.Db.bootstrap
original_seed=h.Db.seed
original_install=h.install_patches
metrics=[]
def bootstrap(self):
 original_bootstrap(self)
 if importlib.util.find_spec('native_pilot.pacing_schema'):
  from native_pilot import pacing_schema
  pacing_schema.upgrade(self.sync_url)
def seed(self,w):
 original_seed(self,w)
 from sqlalchemy.orm import Session
 from db.models import AccountAiConfig
 from db.onlyfans_connection_models import OfProviderConnection
 with Session(self.engine) as s,s.begin():
  for a in w.accts:
   s.get(AccountAiConfig,a).ai_chatter_config_json=json.dumps({'native_reply_pacing_enabled':ON,'native_reply_format_enabled':False})
   s.add(OfProviderConnection(account_id=a,binding_id='binding-'+a,owner_id=int(a[3:]),revision=3,sealed_config='synthetic-unused',expires_at=w.now()+timedelta(days=1),status='verified',verified_at=w.now(),updated_at=w.now(),automate_new_conversations=False,automation_starts_at=w.now()))
def install(stack,holder):
 result=original_install(stack,holder)
 from native_pilot import engine,runner
 previous=engine.NativeEngine
 class CompatibleSyntheticEngine(previous):
  def __init__(self,*a,**kw):
   allowed=inspect.signature(previous.__init__).parameters
   kw={k:v for k,v in kw.items() if k in allowed}
   super().__init__(*a,**kw)
 stack.enter_context(patch.object(engine,'NativeEngine',CompatibleSyntheticEngine))
 original_emit=runner.emit
 def emit(event):
  if event.get('event')=='reply-pacing-receipt':metrics.append(event)
  original_emit(event)
 stack.enter_context(patch.object(runner,'emit',emit))
 if importlib.util.find_spec('native_pilot.pacing'):
  from native_pilot import pacing
  stack.enter_context(patch.object(pacing,'random',random.Random(2031)))
 return result
h.Db.bootstrap=bootstrap;h.Db.seed=seed;h.install_patches=install
h.scenarios=lambda args:{'pacing_steady':h.Cfg(name='pacing_steady',accounts=2,fans=12,sim_minutes=10,scale=10,throttle_s=20,seed=7,rate_per_min=1.5,model_lo_s=3,model_hi_s=3,provider_latency_s=.6)}
def fixed_schedule(w):
 return [(30+45*i+2*k,a,w.fans[i%len(w.fans)]) for i in range(11) for a in w.accts for k in range(2)]
h.make_schedule=lambda w:sorted(fixed_schedule(w))
args=argparse.Namespace(pg_url=None,pg_container=None,scenario='pacing_steady')
report=h.run_all(args)
r=report['results'][0]
r['pacing_test_on']=ON;r['pacing_receipts']=metrics
for account in r['accounts'].values():
 account['model_calls_per_reply']=account['model_calls']/max(1,account['replies_sent'])
Path('/tmp/load-report.json').write_text(json.dumps(report,indent=2,default=str))
print(json.dumps({'totals':r['totals'],'accounts':r['accounts'],'control':r.get('control'),'events':r['event_reasons'],'crashes':r['service_crashed'],'helper_errors':r['helper_errors'],'pacing_receipts':len(metrics)},default=str))
assert not r['service_crashed'] and not r['helper_errors']
assert r['totals']['replies']>=10
assert all(a['throttle_respected'] for a in r['accounts'].values())
assert not any('service-error' in key for key in r['event_reasons'])
assert not ON or metrics
