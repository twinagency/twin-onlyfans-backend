"""Deploy tested profile fixes over verified live bases, preserving other releases."""
import pathlib,json,subprocess,fcntl,os,time,hashlib
P=pathlib.Path('/var/lib/twin-release/memory-accounting-20261102');C=pathlib.Path('/etc/twin-onlyfans/media-agency-release-compose.json')
os.umask(0o077)
def run(a):return subprocess.run(a,check=True,capture_output=True,text=True).stdout
def inspect(n):return json.loads(run(['docker','inspect',n]))[0]
def compose(*a):return run(['docker','compose','-p','twin-onlyfans','-f',str(C),*a])
locks=[]
for path in ['/var/lock/twin-onlyfans-native-release.lock','/etc/twin-onlyfans/paid-media-release.lock']:
 f=open(path,'a');fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);locks.append(f)
tests=(P/'tests-deploy.log').read_text();assert '[100%]' in tests and ' passed' in tests and 'failed' not in tests and 'ERROR' not in tests,'Tests incomplete/failing'
assert inspect('twin-onlyfans-worker-1')['Image']=='sha256:916971197b047cd4daaf460b420179f6f78949b44a813623226749f324727bf9'
assert inspect('twin-onlyfans-api-1')['Image']=='sha256:a40fd1eb18c98c1ae31dd551df2c4314f42198ca76824afcd79e457173aa30ab'
assert not inspect('twin-onlyfans-fastt-supervisor-1')['State']['Running']
# Every edited pre-existing file must still match the reviewed baseline in both services.
for p in (P/'base').rglob('*.py'):
 rel=str(p.relative_to(P/'base'))
 for container in ['twin-onlyfans-worker-1','twin-onlyfans-api-1']:
  assert run(['docker','exec',container,'cat','/app/service/'+rel]).rstrip()==p.read_text().rstrip(),(container,rel)
expected_hashes={'tests/test_profile_quality_accounting.py': '3be3c0603a84c574333485baaa4b1015eb6c450f7d87514fa725360836ec8dec', 'native_pilot/budget.py': '7d4057fb3f2d07747b08336a112e64917af0d89084a41e72879df0581e0cbdaa', 'onlyfans_runtime/profile_learning/storage.py': '2076106428eb15714114cf5ad0004cbb4791f7d5cf289ba4582153e469a459fc', 'onlyfans_runtime/profile_learning/worker.py': 'eaab2db11671e3564ad157d49ab88d7a921e5c78c1feb35f4e35101ccd980bf0', 'onlyfans_runtime/profile_learning/evidence.py': '9bc03fb3e4d0014004cc971a38f6cb8d9e421bb78cf68400e56d611cb2984d45', 'onlyfans_runtime/profile_learning/repair.py': '9b268d70d6cc4fa1d8962d5b1709b8ea2be654371193f92dce3680677aa3b6e8', 'onlyfans_runtime/profile_learning/model.py': '2d094278c838ab6a1d7cf75679e057a3b860fdb1408c62c3b38c340602a9b3a7', 'onlyfans_runtime/profile_learning/accounting.py': '827d74cffb3d86808bb52cd9e0aa975647c7daf635faa998874ccfe3e441e195'}
assert {str(p.relative_to(P/"service")):hashlib.sha256(p.read_text().rstrip().encode()).hexdigest() for p in (P/"service").rglob("*.py")}==expected_hashes,"Source differs from committed release"
original=C.read_bytes();cfg=json.loads(original);(P/'compose.before.json').write_bytes(original)
worker=inspect('twin-memory-accounting:20261102')['Id'];api=inspect('twin-memory-accounting:api-20261102')['Id']
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
