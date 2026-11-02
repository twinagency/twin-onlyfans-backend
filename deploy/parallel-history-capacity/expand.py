import pathlib,json,subprocess,os,fcntl,time
P=pathlib.Path('/var/lib/twin-release/parallel-history-20261101-17211');C=pathlib.Path('/etc/twin-onlyfans/media-agency-release-compose.json')
os.umask(0o077)
def run(a):return subprocess.run(a,check=True,capture_output=True,text=True).stdout
def compose(*a):return run(['docker','compose','-p','twin-onlyfans','-f',str(C),*a])
locks=[]
for path in ['/var/lock/twin-onlyfans-native-release.lock','/etc/twin-onlyfans/paid-media-release.lock']:
 f=open(path,'a');fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);locks.append(f)
assert json.loads((P/'deployment.json').read_text())['deployed']
for n in ['twin-onlyfans-worker-1','twin-onlyfans-api-1','twin-onlyfans-profile-learner','twin-onlyfans-history-importer-1']:
 d=json.loads(run(['docker','inspect',n]))[0];assert d['State']['Running'] and d['RestartCount']==0,n
original=C.read_bytes();cfg=json.loads(original);policy={a:{'rps':2,'inflight':8} for a in ['of-24181859','of-312988353','of-63665785','of-87030857','of-99421145']}
# Preserve Maile's configured ceiling and all adaptive provider backoff state.
policy['of-323358988']={'rps':4,'inflight':16}
for n in ['worker','api','profile-learner','history-importer']:cfg['services'][n]['environment']['ONLYFANS_IMPORT_CAPACITY']=json.dumps(policy,separators=(',',':'))
cfg['services']['history-importer']['logging']={'driver':'json-file','options':{'max-size':'20m','max-file':'3'}}
replacement=(json.dumps(cfg,indent=2)+'\n').encode();(P/'compose.first-stage.json').write_bytes(original)
compose('stop','-t','90','history-importer','profile-learner','worker')
assert C.read_bytes()==original
try:
 tmp=C.with_suffix('.capacity-expand');tmp.write_bytes(replacement);os.replace(tmp,C)
 compose('up','-d','--no-deps','--no-build','--pull','never','api','worker','profile-learner','history-importer')
 assert not json.loads(run(['docker','inspect','twin-onlyfans-fastt-supervisor-1']))[0]['State']['Running']
 result={'at':time.time(),'configured':policy,'note':'Other verified creators get concurrent reads at an initial 2 HTTP requests/sec. Shared backoff and adaptive rate ceilings are retained.'}
 (P/'expanded.json').write_text(json.dumps(result));print(json.dumps(result))
except Exception:
 assert C.read_bytes()==replacement
 C.write_bytes(original);compose('up','-d','--no-deps','--no-build','--pull','never','api','worker','profile-learner','history-importer');raise
