"""Repair native ledger history connector compatibility over exact live images."""
import ast,fcntl,json,os,subprocess
from pathlib import Path
ROOT=Path('/var/tmp/of-history-connector-repair-20261026')
NAMES=['twin-onlyfans-worker-1','twin-onlyfans-api-1','twin-onlyfans-fastt-supervisor-1']
EXPECTED=['sha256:8d50e4fa3c0c0b3942770e61bcea744fb3cb450501e011744275ab9ecc703ca8','sha256:ce1ba4221a97b5a7faa4e438050aeffbae6086aa984d69877e8a520d27a1bf09','sha256:e721d649e631f52a8ea6b642c001f8a2584b8e687baa6c3bd4b91db02867a714']
def run(args,timeout=180):
 p=subprocess.run(args,text=True,capture_output=True,timeout=timeout)
 if p.returncode:raise RuntimeError(args[0]+' failed: '+p.stderr[-1000:])
 return p.stdout.strip()
def inspect(name):return json.loads(run(['docker','inspect',name]))[0]
def private(p,content):
 fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
 with os.fdopen(fd,'w') as f:f.write(content)
ROOT.mkdir(mode=0o700,parents=True,exist_ok=True)
lock=open('/var/lock/twin-onlyfans-native-release.lock','a')
fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
plan=[]
for name,expected in zip(NAMES,EXPECTED):
 live=inspect(name)
 if live['Image']!=expected or not live['State']['Running']:raise RuntimeError('Live image changed: '+name)
 folder=ROOT/name; folder.mkdir(exist_ok=True)
 source=folder/'provider_connector.py'
 run(['docker','cp',name+':/app/service/onlyfans_runtime/provider_connector.py',str(source)])
 old=source.read_text()
 tree=ast.parse(old); cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='NativeProviderConnector')
 method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='transaction_page')
 lines=old.splitlines(keepends=True); before=''.join(lines[method.lineno-1:method.end_lineno])
 after=before.replace('start, end):','start, end, history=False):').replace('0 <= offset <= 10000','0 <= offset <= (1000000000 if history is True else 10000)').replace('or not timedelta(0) <= end - start <= timedelta(days=90)):', 'or type(history) is not bool\n                or start < datetime(1970, 1, 1)\n                or not timedelta(0) <= end - start <= timedelta(days=60000 if history else 90)):')
 if after==before or 'history=False' not in after or 'days=60000' not in after:raise RuntimeError('Unexpected source')
 source.write_text(old.replace(before,after,1))
 (folder/'Dockerfile').write_text('FROM '+expected+'\nCOPY provider_connector.py /app/service/onlyfans_runtime/provider_connector.py\nLABEL com.twin.history-connector-repair="20261026"\n')
 tag='twin-onlyfans:history-connector-20261026-'+name
 run(['docker','build','-q','-t',tag,str(folder)])
 image=run(['docker','image','inspect',tag,'--format','{{.Id}}'])
 check="""import inspect
from datetime import datetime,timedelta
from onlyfans_runtime.provider_connector import NativeProviderConnector
class Fake:
 def _ready(self):pass
 def verify_identity(self):pass
 def _call(self,*args):return args
a=datetime(1970,1,1);b=datetime(2026,10,25);f=Fake()
assert 'history' in inspect.signature(NativeProviderConnector.transaction_page).parameters
assert '/payouts/transactions?' in NativeProviderConnector.transaction_page(f,offset=10020,start=a,end=b,history=True)[1]
for kwargs in [dict(start=a,end=b),dict(start=a,end=b,history='yes'),dict(start=datetime(1969,1,1),end=b,history=True)]:
 try: NativeProviderConnector.transaction_page(f,offset=0,**kwargs)
 except Exception:pass
 else:raise AssertionError('Unbounded default accepted')
assert NativeProviderConnector.transaction_page(f,offset=0,start=b-timedelta(days=7),end=b)
print('history/default boundary checks passed')
"""
 run(['docker','run','--rm','--network','none','-w','/app/service','--entrypoint','python',image,'-c',check])
 cfg=live['Config']['Labels']['com.docker.compose.project.config_files']
 plan.append({'name':name,'id':live['Id'],'image':expected,'candidate':image,'config':cfg,'service':live['Config']['Labels']['com.docker.compose.service']})
private(ROOT/'plan.json',json.dumps(plan))
configs={p['config']:Path(p['config']).read_text() for p in plan}
for i,(cfg,raw) in enumerate(configs.items()):private(ROOT/('compose-before-'+str(i)+'.json'),raw)
for p in plan:
 current=inspect(p['name'])
 if current['Id']!=p['id'] or current['Image']!=p['image']:raise RuntimeError('Concurrent deployment detected')
for cfg,raw in configs.items():
 data=json.loads(raw)
 for p in plan:
  if p['config']==cfg:data['services'][p['service']]['image']=p['candidate']
 private(Path(cfg),json.dumps(data))
deployed=[]
try:
 for p in plan:
  args=['docker','compose','-p','twin-onlyfans','-f',p['config']]
  if 'supervisor' in p['name']:args+=['--profile','fastt-supervisor']
  run(args+['up','-d','--no-deps','--no-build','--pull','never',p['service']])
  current=inspect(p['name'])
  if not current['State']['Running'] or current['Image']!=p['candidate']:raise RuntimeError('Startup failed')
  run(['docker','exec','-w','/app/service',p['name'],'python','-c',"import inspect;from onlyfans_runtime.provider_connector import NativeProviderConnector;assert 'history' in inspect.signature(NativeProviderConnector.transaction_page).parameters"])
  deployed.append(p['name'])
except Exception:
 for cfg,raw in configs.items():private(Path(cfg),raw)
 raise
private(ROOT/'deployed.json',json.dumps(plan))
print(json.dumps({'state':'deployed','services':deployed,'checks':'default 90-day bound, explicit full-history, negative start and invalid flag rejection','images':{p['name']:p['candidate'] for p in plan}}))
