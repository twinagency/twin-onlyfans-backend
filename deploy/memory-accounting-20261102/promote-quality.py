"""Deploy tested profile fixes over verified live bases, preserving other releases."""
import pathlib,json,subprocess,fcntl,os,time,hashlib
P=pathlib.Path('/var/lib/twin-release/memory-accounting-20261102/final-quality');C=pathlib.Path('/etc/twin-onlyfans/media-agency-release-compose.json')
os.umask(0o077)
def run(a):return subprocess.run(a,check=True,capture_output=True,text=True).stdout
def inspect(n):return json.loads(run(['docker','inspect',n]))[0]
def compose(*a):return run(['docker','compose','-p','twin-onlyfans','-f',str(C),*a])
locks=[]
for path in ['/var/lock/twin-onlyfans-native-release.lock','/etc/twin-onlyfans/paid-media-release.lock']:
 f=open(path,'a');fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);locks.append(f)
tests=(P/'tests-deploy.log').read_text();assert '[100%]' in tests and ' passed' in tests and 'failed' not in tests and 'ERROR' not in tests,'Tests incomplete/failing'
assert inspect('twin-onlyfans-worker-1')['Image']=='sha256:1b823e21ee9fd308ee5f86959c99d2417e84d63334e423ff1e43670175fc791c'
assert inspect('twin-onlyfans-api-1')['Image']=='sha256:d95d300aad9dc7352b3aaa664437db62e9f2d43a6cb0aed3a1301f8e8deded0c'
assert not inspect('twin-onlyfans-fastt-supervisor-1')['State']['Running']
# Every edited pre-existing file must still match the reviewed baseline in both services.
for p in (P/'base').rglob('*.py'):
 rel=str(p.relative_to(P/'base'))
 for container in ['twin-onlyfans-worker-1','twin-onlyfans-api-1']:
  assert run(['docker','exec',container,'cat','/app/service/'+rel]).rstrip()==p.read_text().rstrip(),(container,rel)
expected_hashes={'onlyfans_runtime/profile_learning/evidence.py': '0ce40bf9ef5df963c1f0d2c82588488dd891bd0512aa94ffde19dabbc8dd774a', 'tests/test_profile_quality_accounting.py': '1a1ae0056723be54d6f3823b7d25c3e664e5d589b84c7ffd02f44d2b62969389'}
assert {str(p.relative_to(P/"service")):hashlib.sha256(p.read_text().rstrip().encode()).hexdigest() for p in (P/"service").rglob("*.py")}==expected_hashes,"Source differs from committed release"
original=C.read_bytes();cfg=json.loads(original);(P/'compose.before.json').write_bytes(original)
worker=inspect('twin-memory-accounting:quality-20261102')['Id'];api=inspect('twin-memory-accounting:quality-api-20261102')['Id']
for n in ['worker','history-importer','profile-learner','api']:cfg['services'][n]['image']=api if n=='api' else worker
replacement=(json.dumps(cfg,indent=2)+'\n').encode();changed=stopped=False
try:
 compose('stop','-t','90','profile-learner','history-importer','worker');stopped=True
 assert C.read_bytes()==original,'Concurrent release'
 tmp=C.with_suffix('.memory-next');tmp.write_bytes(replacement);os.replace(tmp,C);changed=True
 compose('up','-d','--no-deps','--no-build','--pull','never','api','worker','profile-learner','history-importer')
 for n,i in [('twin-onlyfans-worker-1',worker),('twin-onlyfans-history-importer-1',worker),('twin-onlyfans-profile-learner',worker),('twin-onlyfans-api-1',api)]:
  d=inspect(n);assert d['State']['Running'] and d['Image']==i,n
 assert not inspect('twin-onlyfans-fastt-supervisor-1')['State']['Running']
 result={'deployed':True,'at':time.time(),'workerImage':worker,'apiImage':api,
   'sourceHashes':{str(p.relative_to(P/'service')):hashlib.sha256(p.read_text().rstrip().encode()).hexdigest() for p in (P/'service').rglob('*.py')},
   'existingBudgetCapsAndHoldsPreserved':True,'replySupervisorRunning':False}
 (P/'deployment.json').write_text(json.dumps(result));print(json.dumps(result),flush=True)
except Exception:
 if changed:
  assert C.read_bytes()==replacement,'Concurrent release blocks rollback';C.write_bytes(original)
 if stopped:compose('up','-d','--no-deps','--no-build','--pull','never','api','worker','profile-learner','history-importer')
 raise
