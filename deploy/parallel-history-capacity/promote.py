"""Promote an exact-live-base capacity layer with migration and rollback fences."""
import fcntl,json,os,pathlib,subprocess,time,hashlib,copy
P=pathlib.Path('/var/lib/twin-release/parallel-history-20261101-17211')
C=pathlib.Path('/etc/twin-onlyfans/media-agency-release-compose.json')
os.umask(0o077)
def run(a):return subprocess.run(a,check=True,capture_output=True,text=True).stdout
def inspect(n):return json.loads(run(['docker','inspect',n]))[0]
def compose(*a):return run(['docker','compose','-p','twin-onlyfans','-f',str(C),*a])
locks=[]
for path in ['/var/lock/twin-onlyfans-native-release.lock','/etc/twin-onlyfans/paid-media-release.lock']:
 f=open(path,'a');fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);locks.append(f)
tests=(P/'integrated-tests.log').read_text()
assert ' passed' in tests and 'failed' not in tests and 'ERROR' not in tests and '[100%]' in tests,'Integrated tests not complete/passing'
assert 'ready (0106_parallel_history_reads)' in (P/'live-schema-rehearsal.log').read_text()
assert (P/'database.before.dump').stat().st_size>1000000

for line in (P/'backup.sha256').read_text().splitlines():
 expected,name=line.split(maxsplit=1);h=hashlib.sha256()
 with (P/name).open('rb') as f:
  for part in iter(lambda:f.read(1048576),b''):h.update(part)
 assert h.hexdigest()==expected,'Backup checksum mismatch'

worker=inspect('twin-onlyfans-worker-1');api=inspect('twin-onlyfans-api-1')
assert worker['Image']=='sha256:27bdb4a54aaf8297bfe47579f00b2ce51fad1b765a39b17009b3b5f3a2853e0d'
assert api['Image']=='sha256:ec8590d78606445935f6ef6164242192dde1df2804842f8a0e864992d78ed423'
assert not inspect('twin-onlyfans-fastt-supervisor-1')['State']['Running'],'Reply supervisor must remain stopped'
original=C.read_bytes();cfg=json.loads(original)
(P/'compose.before.json').write_bytes(original)
(P/'containers.before.json').write_text(json.dumps({'worker':worker,'api':api,'learner':inspect('twin-onlyfans-profile-learner')}))
env=dict(x.split('=',1) for x in worker['Config']['Env'])
(P/'migration.env').write_text('ONLYFANS_MIGRATION_URL='+env['DATABASE_URL']+'\n')
newworker=inspect('twin-parallel-history:17211-integrated')['Id'];newapi=inspect('twin-parallel-history:17211-api')['Id']
# First measured stage is Maile. Other accounts retain their existing admission policy.
policy=json.dumps({'of-323358988':{'rps':4,'inflight':16}},separators=(',',':'))
for svc,image in [('worker',newworker),('api',newapi),('profile-learner',newworker)]:
 cfg['services'][svc]['image']=image
 cfg['services'][svc]['environment']['ONLYFANS_IMPORT_CAPACITY']=policy
cfg['services']['worker']['mem_limit']='2g'
learner=cfg['services']['profile-learner'];learner['entrypoint']=['python'];learner['command']=['-m','onlyfans_runtime.profile_learning.worker'];learner['mem_limit']='1g';learner['cpus']='1.0'
learner['environment']['ONLYFANS_PROFILE_CONCURRENCY']='16';learner['environment']['ONLYFANS_PROFILE_SEED_BATCH']='5000'
importer=copy.deepcopy(cfg['services']['worker'])
importer.pop('container_name',None);importer.pop('ports',None);importer.pop('healthcheck',None)
importer['entrypoint']=['python'];importer['command']=['-m','onlyfans_runtime.parallel_history'];importer['mem_limit']='2g';importer['cpus']='2.0';importer['restart']='unless-stopped'
importer['environment']['ONLYFANS_PARALLEL_IMPORT_ENABLED']='1';importer['environment']['ONLYFANS_IMPORT_WORKERS']='64'
cfg['services']['history-importer']=importer
# Preserve the DB resource allocation actually running, rather than the stale manifest.
db=inspect('twin-onlyfans-database-1');cfg['services']['database']['mem_limit']=str(db['HostConfig']['Memory']);cfg['services']['database']['command']=db['Config']['Cmd']
replacement=(json.dumps(cfg,indent=2)+'\n').encode();(P/'compose.next.json').write_bytes(replacement)
migrated=changed=stopped=False
try:
 compose('stop','-t','90','worker','api','profile-learner');stopped=True
 assert C.read_bytes()==original,'Concurrent deployment changed manifest'
 out=run(['docker','run','--rm','--network','host','--env-file',str(P/'migration.env'),'--entrypoint','python',newworker,'-m','db.schema','upgrade'])
 (P/'production-migration.log').write_text(out);assert 'ready (0106_parallel_history_reads)' in out;migrated=True
 tmp=C.with_suffix('.capacity-next');tmp.write_bytes(replacement);os.replace(tmp,C);changed=True
 compose('up','-d','--no-deps','--no-build','--pull','never','api','worker','profile-learner','history-importer')
 for n,image in [('twin-onlyfans-worker-1',newworker),('twin-onlyfans-api-1',newapi),('twin-onlyfans-profile-learner',newworker),('twin-onlyfans-history-importer-1',newworker)]:
  d=inspect(n);assert d['State']['Running'] and d['Image']==image,n
 assert not inspect('twin-onlyfans-fastt-supervisor-1')['State']['Running']
 result={'deployed':True,'at':time.time(),'workerImage':newworker,'apiImage':newapi,'schema':'0106_parallel_history_reads','initialCapacity':json.loads(policy),'memoryConcurrency':16,'historyWorkers':64,'replySupervisorRunning':False}
 (P/'deployment.json').write_text(json.dumps(result));print(json.dumps(result),flush=True)
except Exception:
 if changed:compose('stop','-t','90','history-importer','profile-learner','worker','api')
 if migrated:
  script="import os;from sqlalchemy import create_engine;from db.schema import _config;from alembic import command;e=create_engine(os.environ['ONLYFANS_MIGRATION_URL']);c=e.connect();t=c.begin();command.downgrade(_config(c),'0105_earnings_totals');t.commit();c.close();e.dispose()"
  run(['docker','run','--rm','--network','host','--env-file',str(P/'migration.env'),'--entrypoint','python',newworker,'-c',script])
 if changed:
  assert C.read_bytes()==replacement,'Concurrent deployment prevents rollback'
  C.write_bytes(original)
 if stopped:compose('up','-d','--no-deps','--no-build','--pull','never','api','worker')
 raise
