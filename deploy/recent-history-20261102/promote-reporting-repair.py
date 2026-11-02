"""Promote qualified reporting-only images; preserve pilot cutoff and controls."""
import copy,fcntl,json,os,pathlib,subprocess,sys,time
os.umask(0o077)
P=pathlib.Path('/var/lib/twin-release/recent-history-20261102-resume')
REV=sys.argv[1];B=P/('repair-'+REV[:12])
C=pathlib.Path('/etc/twin-onlyfans/media-agency-release-compose.json')
def run(a,**kw):return subprocess.run(a,check=True,capture_output=True,text=True,**kw).stdout
def inspect(n):return json.loads(run(['docker','inspect',n]))[0]
def compose(*a):return run(['docker','compose','-p','twin-onlyfans','-f',str(C),*a])
locks=[]
for p in ['/var/lock/twin-onlyfans-native-release.lock','/etc/twin-onlyfans/paid-media-release.lock']:
 f=open(p,'a');fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);locks.append(f)
assert not (P/'deployment-repair.json').exists(),'Do not replay promotion'
manifest=json.loads((B/'build.json').read_text());q=json.loads((B/'qualification.json').read_text())
assert manifest['revision']==REV and q['revision']==REV and q['exitCode']==0 and ' passed' in q['tail']
original=C.read_bytes();assert original==(P/'compose.pilot.private.json').read_bytes(),'Concurrent configuration changed'
assert json.loads((P/'pilot.recovered-before.private.json').read_text())['baselineProvenance']['originalSnapshotWasEmpty']
old={n:inspect(n) for n in ['twin-onlyfans-worker-1','twin-onlyfans-api-1','twin-onlyfans-history-importer-1','twin-onlyfans-profile-learner','twin-onlyfans-fastt-supervisor-1','next-app-of','next-app']}
for role in ['worker','api']:assert old['twin-onlyfans-'+role+'-1']['Image']==manifest['images'][role]['base']
assert not old['twin-onlyfans-fastt-supervisor-1']['State']['Running']
check='''import os,json
from sqlalchemy.orm import Session
from sqlalchemy import text
from onlyfans_runtime.store import Store
s=Store(os.environ['DATABASE_URL'])
try:
 with Session(s.engine) as db:
  assert not s._runtime(db,os.environ['ONLYFANS_WORKER_TENANT'],'of-323358988',lock=False).enabled
  print(json.dumps(list(db.execute(text("SELECT binding_id,cutoff_at::text FROM of_history_policy WHERE account_id='of-323358988'")).one())))
finally:s.close()
'''
policy_before=json.loads(run(['docker','exec','twin-onlyfans-worker-1','python','-c',check]))
(P/'compose.before-reporting-repair.private.json').write_bytes(original)
(P/'containers.before-reporting-repair.private.json').write_text(json.dumps(old))
cfg=json.loads(original);images={r:manifest['images'][r]['image'] for r in ['worker','api']}
for svc,role in [('worker','worker'),('api','api'),('history-importer','worker'),('profile-learner','worker'),('migrate','worker')]:
 if svc in cfg['services']:cfg['services'][svc]['image']=images[role]
started=time.time()
try:
 compose('stop','-t','90','history-importer','profile-learner','worker','api')
 assert C.read_bytes()==original,'Concurrent configuration changed'
 tmp=C.with_suffix('.reporting-repair-next');tmp.write_text(json.dumps(cfg,indent=2)+'\n');os.replace(tmp,C)
 compose('up','-d','--no-deps','--no-build','--pull','never','api','worker')
 for _ in range(30):
  if subprocess.run(['docker','exec','twin-onlyfans-api-1','python','-c',"import urllib.request;assert urllib.request.urlopen('http://127.0.0.1:8787/healthz',timeout=2).status==200"],capture_output=True).returncode==0:break
  time.sleep(1)
 else:raise RuntimeError('API health failed')
 assert json.loads(run(['docker','exec','twin-onlyfans-worker-1','python','-c',check]))==policy_before
 for role in ['worker','api']:
  n='twin-onlyfans-'+role+'-1';d=inspect(n)
  assert d['State']['Running'] and d['Image']==images[role]
  assert sorted(d['Config']['Env'])==sorted(old[n]['Config']['Env']),'Environment changed'
 for n in ['next-app-of','next-app','twin-onlyfans-fastt-supervisor-1']:assert inspect(n)['Id']==old[n]['Id']
 compose('up','-d','--no-deps','--no-build','--pull','never','history-importer','profile-learner')
 for n in ['twin-onlyfans-history-importer-1','twin-onlyfans-profile-learner']:
  d=inspect(n);assert d['Image']==images['worker'] and d['State']['Running']
  assert sorted(d['Config']['Env'])==sorted(old[n]['Config']['Env']),'Job environment changed'
 rollback=copy.deepcopy(cfg)
 for svc in ['worker','api','history-importer','profile-learner','migrate']:
  if svc in rollback['services']:rollback['services'][svc]['environment']['ONLYFANS_RECENT_HISTORY_ACCOUNTS']='[]'
 (P/'rollback.flag-off.private.json').write_text(json.dumps(rollback,indent=2)+'\n')
 result={'deployed':True,'sourceCommit':REV,'images':images,'deployedAt':time.time(),
  'deploymentInterruptionSeconds':time.time()-started,'schemaChanged':False,'cutoffChanged':False,
  'aiRepliesEnabled':False,'dashboardReplaced':False,'baselineCapturedBeforePilotJobs':False,
  'baselineReconstructedFromPrePilotBackup':True,'originalManifestBaselineClaimCorrectedHere':True}
 (P/'deployment-repair.json').write_text(json.dumps(result,indent=2));print(json.dumps(result),flush=True)
except BaseException:
 compose('stop','-t','90','history-importer','profile-learner','worker','api')
 C.write_bytes(original)
 compose('up','-d','--no-deps','--no-build','--pull','never','api','worker','history-importer','profile-learner')
 raise
