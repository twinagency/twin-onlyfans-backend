"""Increase the resumed learner's cgroup limit without restarting its requests."""
import pathlib,json,subprocess,fcntl,os,time
os.umask(0o077)
P=pathlib.Path('/var/lib/twin-release/memory-accounting-20261102/legacy-bound')
C=pathlib.Path('/etc/twin-onlyfans/media-agency-release-compose.json')
locks=[]
for path in ['/var/lock/twin-onlyfans-native-release.lock','/etc/twin-onlyfans/paid-media-release.lock']:
 f=open(path,'a');fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);locks.append(f)
def inspect():return json.loads(subprocess.run(['docker','inspect','twin-onlyfans-profile-learner'],capture_output=True,text=True,check=True).stdout)[0]
before=inspect();assert before['State']['Running']
available=int(next(l.split()[1] for l in pathlib.Path('/proc/meminfo').read_text().splitlines() if l.startswith('MemAvailable:')))*1024
assert available>=3*1024**3,'Insufficient host headroom'
assert before['HostConfig']['Memory']==1024**3,'Unexpected existing limit'
original=C.read_bytes();cfg=json.loads(original);service=cfg['services']['profile-learner'];service['mem_limit']='2g'
if 'deploy' in service and 'limits' in service['deploy'].get('resources',{}):service['deploy']['resources']['limits']['memory']='2g'
replacement=(json.dumps(cfg,indent=2)+'\n').encode();(P/'compose.before-headroom.json').write_bytes(original)
assert C.read_bytes()==original,'Concurrent config change'
tmp=C.with_suffix('.learner-headroom');tmp.write_bytes(replacement);os.replace(tmp,C)
try:
 subprocess.run(['docker','update','--memory','2g','--memory-swap','2g','twin-onlyfans-profile-learner'],capture_output=True,text=True,check=True)
 after=inspect();assert after['HostConfig']['Memory']==2*1024**3 and after['State']['Running']
 assert after['State']['StartedAt']==before['State']['StartedAt'] and after['Image']==before['Image'],'Unexpected restart/image change'
 result={'at':time.time(),'memory_before':before['HostConfig']['Memory'],'memory_after':after['HostConfig']['Memory'],
  'host_available_bytes':available,'restart_required':False,'restarts':after['RestartCount'],'image':after['Image']}
 (P/'headroom.json').write_text(json.dumps(result));print(json.dumps(result))
except Exception:
 assert C.read_bytes()==replacement,'Concurrent config change prevents restore'
 C.write_bytes(original);raise
