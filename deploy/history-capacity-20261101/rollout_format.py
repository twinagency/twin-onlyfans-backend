"""Promote the tested history-only worker overlay with exact-base checks."""
import fcntl, hashlib, json, os, subprocess
from pathlib import Path

ROOT=Path('/var/lib/twin-release/history-capacity-20261101-e1c')
CONFIG=Path('/etc/twin-onlyfans/media-agency-release-compose.json')
NAME='twin-onlyfans-worker-1'
BASE='sha256:2c2646295cd31bc6166b580f2db8042cb1a7714e8830cf1bbcdcecc0db526a9f'
def run(args):return subprocess.run(args,check=True,capture_output=True,text=True).stdout
def inspect(name):return json.loads(run(['docker','inspect',name]))[0]
def compose(*args):return run(['docker','compose','-p','twin-onlyfans','-f',str(CONFIG),*args])
os.umask(0o077)
locks=[]
for path in ['/var/lock/twin-onlyfans-native-release.lock','/etc/twin-onlyfans/paid-media-release.lock']:
 f=open(path,'a');fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);locks.append(f)
assert '154 passed' in (ROOT/'format-tests.log').read_text()
assert '1 passed' in (ROOT/'html-pg-test.log').read_text()
pg=(ROOT/'pg-tests.log').read_text()
assert 'passed' in pg and 'failed' not in pg and 'ERROR' not in pg,pg[-200:]
before=inspect(NAME);assert before['State']['Running'] and before['Image']==BASE,'Live worker changed; refresh required'
original=CONFIG.read_bytes();config=json.loads(original)
assert inspect(config['services']['worker']['image'])['Id']==BASE,'Manifest/live drift'
image=inspect('twin-history-capacity:e1c-format')['Id']
config['services']['worker']['image']=image
replacement=(json.dumps(config,indent=2)+'\n').encode()
(ROOT/'compose.format.before.json').write_bytes(original)
(ROOT/'worker.format.before.json').write_text(json.dumps(before))
stopped=changed=False
try:
 compose('stop','-t','60','worker');stopped=True
 assert CONFIG.read_bytes()==original,'Concurrent deployment'
 tmp=CONFIG.with_suffix('.history-next');tmp.write_bytes(replacement);os.replace(tmp,CONFIG);changed=True
 compose('up','-d','--no-deps','--no-build','--pull','never','worker')
 after=inspect(NAME)
 assert after['State']['Running'] and after['Image']==image
 env=lambda d:dict(v.split('=',1) for v in d['Config']['Env'])
 assert env(after)==env(before),'Environment changed'
 for key in ['Cmd','Entrypoint','User','WorkingDir']:
  assert after['Config'].get(key)==before['Config'].get(key),key
 for key in ['NetworkMode','RestartPolicy','Memory','NanoCpus']:
  assert after['HostConfig'].get(key)==before['HostConfig'].get(key),key
 assert sorted(after['HostConfig'].get('Binds') or [])==sorted(before['HostConfig'].get('Binds') or []),'Mount values changed'
 mounts=lambda d:sorted((m['Type'],m['Source'],m['Destination'],m['RW']) for m in d['Mounts'])
 assert mounts(after)==mounts(before),'Effective mounts changed'
 for rel in ['onlyfans_runtime/native_worker.py','onlyfans_runtime/launch_history_memory/recovery.py','onlyfans_runtime/native_protocol.py']:
  raw=run(['docker','exec',NAME,'cat','/app/service/'+rel])
  assert raw==(ROOT/'service'/rel).read_text(),'Source mismatch'
 result={'deployed':True,'image':image,'base':BASE,'revision':'98889209f3c8dbe1ed052d7f2cfa99212d217c96','environment_preserved':True}
 (ROOT/'deployment.json').write_text(json.dumps(result));print(json.dumps(result))
except Exception:
 if changed:
  assert CONFIG.read_bytes()==replacement,'Concurrent deployment prevents rollback'
  CONFIG.write_bytes(original)
 if stopped:compose('up','-d','--no-deps','--no-build','--pull','never','worker')
 raise
