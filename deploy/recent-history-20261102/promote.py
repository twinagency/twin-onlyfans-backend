"""Guarded Maile-only backend pilot. Keep dashboards and reply supervisor intact.

The old API contract stays valid. Flag-off rollback uses the new schema-compatible
images and stops the pilot jobs; it never deletes data or restores an old DB dump.
"""
import copy,fcntl,hashlib,json,os,pathlib,shutil,subprocess,sys,time
os.umask(0o077)
HERE=pathlib.Path(__file__).resolve().parent
P=pathlib.Path('/var/lib/twin-release/recent-history-20261102-resume')
B=P/'build-9870';C=pathlib.Path('/etc/twin-onlyfans/media-agency-release-compose.json')
A='of-323358988';REV='9870b64889833b16a36397c8ffa45dc7e236cea3'
def run(a,**kw):return subprocess.run(a,check=True,capture_output=True,text=True,**kw).stdout
def inspect(n):return json.loads(run(['docker','inspect',n]))[0]
def compose(*a):return run(['docker','compose','-p','twin-onlyfans','-f',str(C),*a])
locks=[]
for p in ['/var/lock/twin-onlyfans-native-release.lock','/etc/twin-onlyfans/paid-media-release.lock']:
 f=open(p,'a');fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);locks.append(f)
assert not (P/'deployment.json').exists(),'Review the previous deployment instead of replaying it'
manifest=json.loads((B/'build.json').read_text());q=json.loads((B/'qualification.json').read_text())
assert manifest['revision']==REV and q['revision']==REV and q['exitCode']==0
assert ' passed' in q['tail'] and 'failed' not in q['tail'] and 'ERROR' not in q['tail']
assert 'ready (0110_recent_history)' in pathlib.Path('/var/lib/twin-release/recent-history-20261102-stage/live-schema-rehearsal.log').read_text()
original=C.read_bytes();assert hashlib.sha256(original).hexdigest()=='d34a17d31159243e6afa71628ec0d16b623ec67202f1d48d58bb4f2c9a0eb882','Concurrent manifest changed'
old={n:inspect(n) for n in ['twin-onlyfans-worker-1','twin-onlyfans-api-1','twin-onlyfans-history-importer-1','twin-onlyfans-profile-learner','twin-onlyfans-fastt-supervisor-1','next-app-of','next-app']}
for role in ['worker','api']:assert old['twin-onlyfans-'+role+'-1']['Image']==manifest['images'][role]['base']
for n in ['twin-onlyfans-history-importer-1','twin-onlyfans-profile-learner','twin-onlyfans-fastt-supervisor-1']:assert not old[n]['State']['Running']
assert old['next-app-of']['Image']=='sha256:22be6bc89bf7308e2c2214e03d63313bfc32f2edb6c831bddcd1a06a28258049'
check=(HERE/'legacy-ui-check.cjs').read_text()
ui=json.loads(run(['docker','exec','next-app-of','node','-e',check]));(P/'legacy-ui-check.json').write_text(json.dumps(ui))
(P/'compose.pre-pilot.private.json').write_bytes(original)
(P/'containers.pre-pilot.private.json').write_text(json.dumps(old))
assert shutil.disk_usage(P).free>2*1024**3,'Insufficient backup space'
backup=P/'database.pre-pilot.dump';assert not backup.exists()
with backup.open('wb') as stream:
 subprocess.run(['docker','exec','twin-onlyfans-database-1','sh','-c','pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --no-owner --no-privileges'],check=True,stdout=stream,stderr=subprocess.PIPE,timeout=900)
assert backup.stat().st_size>1000000
h=hashlib.sha256()
with backup.open('rb') as stream:
 for part in iter(lambda:stream.read(1048576),b''):h.update(part)
(P/'pre-pilot-backup.sha256').write_text(h.hexdigest()+' database.pre-pilot.dump\n')
env=dict(x.split('=',1) for x in old['twin-onlyfans-worker-1']['Config']['Env'])
assert not any('\n' in v for v in env.values())
capacity=json.loads(env.get('ONLYFANS_IMPORT_CAPACITY','{}'));assert capacity[A]=={'rps':2,'inflight':8}
# Explicit user request: pause Maile using the existing audited account control.
pause='''import os,json,hashlib
from sqlalchemy.orm import Session
from onlyfans_runtime.store import Store
from onlyfans_runtime.launch_operations import account_state,control_account
s=Store(os.environ['DATABASE_URL']);tenant=os.environ['ONLYFANS_WORKER_TENANT'];account='of-323358988'
try:
 with Session(s.engine) as db:state=account_state(db,s._runtime(db,tenant,account,lock=False))
 result=control_account(s,tenant,account,action='pause',command_key=hashlib.sha256(b'recent-history-maile-pilot-9870').hexdigest(),actor='worker-import:'+hashlib.sha256(b'recent-history-authorized-pilot').hexdigest(),reason='User-authorized 30-day onboarding pilot; AI replies remain disabled',expected_fingerprint=state['fingerprint'])
 print(json.dumps(result))
finally:s.close()
'''
(P/'maile-pause.json').write_text(run(['docker','exec','-i','twin-onlyfans-worker-1','python','-'],input=pause))
cfg=json.loads(original);images={r:manifest['images'][r]['image'] for r in ['worker','api']}
for svc,role in [('worker','worker'),('api','api'),('history-importer','worker'),('profile-learner','worker'),('migrate','worker')]:
 if svc not in cfg['services']:continue
 service=cfg['services'][svc];service['image']=images[role]
 service.setdefault('environment',{})['ONLYFANS_RECENT_HISTORY_ACCOUNTS']=json.dumps([A])
cfg['services']['history-importer']['environment']['ONLYFANS_IMPORT_CAPACITY']=json.dumps({A:capacity[A]})
cfg['services']['profile-learner']['environment']['ONLYFANS_PROFILE_ACCOUNTS']=json.dumps([A])
rollback=copy.deepcopy(cfg)
for svc in ['worker','api','history-importer','profile-learner','migrate']:
 if svc in rollback['services']:rollback['services'][svc].setdefault('environment',{})['ONLYFANS_RECENT_HISTORY_ACCOUNTS']='[]'
(P/'rollback.flag-off.private.json').write_text(json.dumps(rollback,indent=2)+'\n')
(P/'compose.pilot.private.json').write_text(json.dumps(cfg,indent=2)+'\n')
private_env=P/'pilot.private.env';pilot_env={**env,'ONLYFANS_RECENT_HISTORY_ACCOUNTS':json.dumps([A]),'ONLYFANS_MIGRATION_URL':env['DATABASE_URL']}
private_env.write_text(''.join(k+'='+v+'\n' for k,v in pilot_env.items()))
# Existing Docker network, no host port and no authority to increase limits.
networks=list(old['twin-onlyfans-worker-1']['NetworkSettings']['Networks']);assert len(networks)==1
base=['docker','run','--rm','-i','--network',networks[0],'--memory','1g','--cpus','1','--env-file',str(private_env),'--entrypoint','python',images['worker']]
migrated=False;changed=False
try:
 compose('stop','-t','90','worker','api')
 assert C.read_bytes()==original,'Concurrent release changed configuration'
 out=run(base+['-m','db.schema','upgrade']);assert 'ready (0110_recent_history)' in out;migrated=True
 (P/'production-migration.log').write_text(out)
 setup='''import os,json
from sqlalchemy.orm import Session
from onlyfans_runtime.store import Store
from onlyfans_runtime.launch_history_memory.recovery import HistoryRecovery
from onlyfans_runtime.connections import get_connection
from tools.history_pilot_measurement import capture
s=Store(os.environ['DATABASE_URL']);t=os.environ['ONLYFANS_WORKER_TENANT'];a='of-323358988'
try:
 with Session(s.engine) as db:assert not s._runtime(db,t,a,lock=False).enabled
 HistoryRecovery(s).request_account(t,a)
 baseline=capture(s,t,a)
 baseline['phaseProgress']=HistoryRecovery(s).progress(t,a)
 from onlyfans_runtime.launch_history_memory.recovery import OfRecentHistoryCursor
 from sqlalchemy import select,func
 with Session(s.engine) as db:
  baseline['historyPageCanonicalAdditions']=int(db.scalar(select(func.coalesce(func.sum(OfRecentHistoryCursor.messages_added),0)).where(OfRecentHistoryCursor.account_id==a)))
 old=get_connection(s,t,a,include_sync=True,include_runner=True,include_policy=True)
 assert 'recentHistory' not in old and not old['outgoingEnabled'] and old['sync']['recoveryRequired'] and old['sync']['lastCompleteSyncAt'] is None
 print(json.dumps(baseline))
finally:s.close()
'''
 baseline_text=run(base+['-'],input=setup)
 baseline=json.loads(baseline_text)  # Empty stdin/output must abort before any pilot job starts.
 assert baseline['schemaVersion']=='of-history-pilot-snapshot.v1' and baseline['accountId']==A
 assert all(k in baseline for k in ['sources','receipts','modelCalls','cutoffAt','historyPageCanonicalAdditions'])
 (P/'pilot.before.private.json').write_text(baseline_text)
 tmp=C.with_suffix('.recent-history-next');tmp.write_text(json.dumps(cfg,indent=2)+'\n');os.replace(tmp,C);changed=True
 compose('up','-d','--no-deps','--no-build','--pull','never','api','worker')
 for _ in range(30):
  r=subprocess.run(['docker','exec','twin-onlyfans-api-1','python','-c',"import urllib.request;assert urllib.request.urlopen('http://127.0.0.1:8787/healthz',timeout=2).status==200"],capture_output=True)
  if r.returncode==0:break
  time.sleep(1)
 else:raise RuntimeError('API health unavailable')
 for role in ['worker','api']:
  d=inspect('twin-onlyfans-'+role+'-1');assert d['Image']==images[role] and d['State']['Running']
  actual=dict(x.split('=',1) for x in d['Config']['Env']);prior=dict(x.split('=',1) for x in old['twin-onlyfans-'+role+'-1']['Config']['Env'])
  for k,v in prior.items():assert k=='ONLYFANS_RECENT_HISTORY_ACCOUNTS' or actual.get(k)==v,'Runtime environment drift: '+k
 assert not inspect('twin-onlyfans-fastt-supervisor-1')['State']['Running']
 for name in ['next-app-of','next-app']:assert inspect(name)['Id']==old[name]['Id'],'Dashboard changed'
 compose('up','-d','--no-deps','--no-build','--pull','never','history-importer','profile-learner')
 result={'deployed':True,'sourceCommit':REV,'deployedAt':time.time(),'images':images,'schema':'0110_recent_history','account':A,'aiRepliesEnabled':False,'capacity':capacity[A],'baselineCapturedBeforePilotJobs':True,'dashboardReplaced':False,'legacyUiCheck':ui,'backupBytes':backup.stat().st_size,'backupSha256':h.hexdigest(),'measurementComplete':False}
 (P/'deployment.json').write_text(json.dumps(result,indent=2));print(json.dumps(result),flush=True)
except BaseException:
 if changed:compose('stop','-t','90','history-importer','profile-learner','worker','api')
 if migrated:
  C.write_text(json.dumps(rollback,indent=2)+'\n')
 elif changed:C.write_bytes(original)
 compose('up','-d','--no-deps','--no-build','--pull','never','api','worker')
 (P/'deployment-failed.json').write_text(json.dumps({'migrated':migrated,'flagOffRollback':migrated,'aiRemainsPaused':True}))
 raise
