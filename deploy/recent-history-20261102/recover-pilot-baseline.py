"""Reconstruct source/receipt/call evidence from the pre-pilot dump in isolation.

No production writes, provider/model calls or production network are permitted.
The original empty snapshot is retained. Timing bounds and the live-arrival gap
are explicit; this is not a successfully captured start-of-pilot snapshot.
"""
import hashlib,json,os,pathlib,subprocess,sys,time,uuid
from datetime import datetime,timezone
os.umask(0o077)
P=pathlib.Path('/var/lib/twin-release/recent-history-20261102-resume')
REV=sys.argv[1];B=P/('repair-'+REV[:12])
image=json.loads((B/'build.json').read_text())['images']['worker']['image']
deployment=json.loads((P/'deployment.json').read_text())
backup=P/'database.pre-pilot.dump'
assert (P/'pilot.before.private.json').stat().st_size==0
target=P/'pilot.recovered-before.private.json';assert not target.exists()
with backup.open('rb') as f:assert hashlib.file_digest(f,'sha256').hexdigest()==deployment['backupSha256']
lower=(P/'containers.pre-pilot.private.json').stat().st_mtime
upper=backup.stat().st_mtime
assert lower<=upper<deployment['deployedAt']
def run(a,**kw):return subprocess.run(a,check=True,capture_output=True,text=True,**kw).stdout
scope=json.loads(run(['docker','exec','twin-onlyfans-worker-1','python','-c','''import json,os
from sqlalchemy import text
from sqlalchemy.orm import Session
from onlyfans_runtime.store import Store
s=Store(os.environ['DATABASE_URL']);t=os.environ['ONLYFANS_WORKER_TENANT'];a='of-323358988'
try:
 with Session(s.engine) as db:
  assert not s._runtime(db,t,a,lock=False).enabled
  b,start,cutoff=db.execute(text('SELECT binding_id,started_at::text,cutoff_at::text FROM of_history_policy WHERE account_id=:a'),{'a':a}).one()
  print(json.dumps({'tenant':t,'account':a,'binding':b,'started':start,'cutoff':cutoff}))
finally:s.close()
''']))
network='recent-evidence-'+uuid.uuid4().hex[:8];db=network+'-db'
run(['docker','network','create','--internal',network])
try:
 run(['docker','run','-d','--name',db,'--network',network,'--memory','1g','--cpus','1',
      '-v',str(backup)+':/backup.dump:ro','-e','POSTGRES_USER=synthetic','-e','POSTGRES_PASSWORD=synthetic',
      '-e','POSTGRES_DB=postgres','ca0bd484cb98'])
 for _ in range(60):
  if subprocess.run(['docker','exec',db,'pg_isready','-U','synthetic','-d','postgres'],capture_output=True).returncode==0:break
  time.sleep(.25)
 else:raise RuntimeError('Isolated evidence database unavailable')
 with (P/'baseline-restore.private.log').open('w') as out:
  subprocess.run(['docker','exec',db,'pg_restore','-U','synthetic','-d','postgres','--no-owner','--no-privileges','--exit-on-error','/backup.dump'],check=True,stdout=out,stderr=subprocess.STDOUT)
 url='postgresql+psycopg2://synthetic:synthetic@'+db+':5432/postgres'
 base=['docker','run','--rm','-i','--network',network,'--memory','1g','--cpus','1','--entrypoint','python',
       '-e','DATABASE_URL='+url,'-e','ONLYFANS_MIGRATION_URL='+url,'-e','ONLYFANS_PROCESS_ROLE=worker',
       '-e','ONLYFANS_RECENT_HISTORY_ACCOUNTS='+json.dumps([scope['account']]),image]
 (P/'baseline-migration.log').write_text(run(base+['-m','db.schema','upgrade']))
 code='''import json,os,sys
from sqlalchemy import text
from tools.history_pilot_measurement import capture
from onlyfans_runtime.store import Store
d=json.load(sys.stdin);s=Store(os.environ['DATABASE_URL'])
try:
 with s.engine.begin() as c:
  c.execute(text('INSERT INTO of_history_policy(account_id,binding_id,enabled,started_at,cutoff_at,timestamp_order_verified,writing_sample_limit,updated_at) VALUES (:account,:binding,true,:started,:cutoff,false,0,:started)'),d)
  c.execute(text('ANALYZE messages'));c.execute(text('ANALYZE of_ingress'));c.execute(text('ANALYZE of_profile_receipts'))
 snapshot=capture(s,d['tenant'],d['account']);snapshot['historyPageCanonicalAdditions']=0
 assert snapshot['recentHttpAttempts']==0
 print(json.dumps(snapshot))
finally:s.close()
'''
 snapshot=json.loads(run(base+['-c',code],input=json.dumps(scope)))
 snapshot['observedAt']=datetime.fromtimestamp(lower,timezone.utc).isoformat()
 provenance={'source':'pre-pilot database dump reconstructed in an isolated database',
  'backupSha256':deployment['backupSha256'],'snapshotTimeLowerBound':snapshot['observedAt'],
  'snapshotTimeUpperBound':datetime.fromtimestamp(upper,timezone.utc).isoformat(),
  'pilotPolicyStartedAt':scope['started']+'Z','originalSnapshotWasEmpty':True,
  'notFreshOnboarding':True,'newSourcesMayIncludeLiveArrivalsBetweenBackupAndPilot':True,
  'elapsedComparisonIncludesBackupToActivationGap':True,'productionWrites':False}
 snapshot['baselineProvenance']=provenance
 with target.open('x') as f:json.dump(snapshot,f)
 (P/'baseline-reconstruction.json').write_text(json.dumps(provenance,indent=2))
 print(json.dumps({'baselineRecovered':True,'windowSources':len(snapshot['sources']),
  'completedIncomingReceipts':len(snapshot['receipts']),'modelCallRecords':len(snapshot['modelCalls']),
  'provenance':provenance}),flush=True)
finally:
 subprocess.run(['docker','rm','-fv',db],capture_output=True)
 subprocess.run(['docker','network','rm',network],capture_output=True)
