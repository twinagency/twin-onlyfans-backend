"""Read pilot results through the running worker; keep private fingerprints on host."""
import json,pathlib,subprocess
P=pathlib.Path('/var/lib/twin-release/recent-history-20261102-resume')
assert (P/'deployment.json').exists(),'Pilot has not deployed'
baseline=P/'pilot.before.private.json'
if baseline.stat().st_size==0:
 baseline=P/'pilot.recovered-before.private.json'
before=baseline.read_text()
assert json.loads(before)['schemaVersion']=='of-history-pilot-snapshot.v1'
script='''import json,os,sys
from sqlalchemy import func,select
from sqlalchemy.orm import Session
from onlyfans_runtime.store import Store
from onlyfans_runtime.launch_history_memory.recovery import OfRecentHistoryCursor,HistoryRecovery
from tools.history_pilot_measurement import capture,compare
from tools.history_onboarding_report import report
before=json.load(sys.stdin);store=Store(os.environ['DATABASE_URL']);t=os.environ['ONLYFANS_WORKER_TENANT'];a='of-323358988'
try:
 with Session(store.engine) as db:assert not store._runtime(db,t,a,lock=False).enabled,'Maile replies must remain off'
 after=capture(store,t,a)
 with Session(store.engine) as db:
  after['historyPageCanonicalAdditions']=int(db.scalar(select(func.coalesce(func.sum(OfRecentHistoryCursor.messages_added),0)).where(OfRecentHistoryCursor.account_id==a)))
 comparison=compare(before,after)
 comparison['baselineProvenance']=before.get('baselineProvenance',{'source':'captured before pilot jobs'})
 comparison['newCanonicalMessagesSavedByHistoryPages']=after['historyPageCanonicalAdditions']-before['historyPageCanonicalAdditions']
 # New source keys include concurrent live arrivals, unlike the page-commit counter.
 print(json.dumps({'privateSnapshot':after,'publicReport':{'pilot':comparison,'coverage':report(store,t,a),'aiRepliesEnabled':False}}))
finally:store.close()
'''
result=subprocess.run(['docker','exec','-i','twin-onlyfans-worker-1','python','-c',script],input=before,check=True,capture_output=True,text=True)
d=json.loads(result.stdout)
# Exclusive timestamped readbacks retain each checkpoint and the original baseline.
name=d['privateSnapshot']['observedAt'].replace(':','-').replace('+','_')
private=P/('pilot.after.'+name+'.private.json')
with private.open('x') as f:json.dump(d['privateSnapshot'],f)
private.chmod(0o600)
public=P/('pilot.measurement.'+name+'.json');public.write_text(json.dumps(d['publicReport'],indent=2))
print(json.dumps(d['publicReport']),flush=True)
