import json,pathlib,subprocess,hashlib,os,time,fcntl
ROOT=pathlib.Path('/var/lib/twin-release/bulk-ai-20261030');NAME='twin-onlyfans-api-1'
manifest=pathlib.Path('/etc/twin-onlyfans/media-agency-release-compose.json')
prepared=json.loads((ROOT/'prepared.json').read_text());tests=json.loads((ROOT/'tests.json').read_text())
assert tests['exit']==0 and tests['application_image']==prepared['candidate_image']
lock=open(ROOT/'cutover.lock','a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
def run(args,**kw):return subprocess.check_output(args,text=True,**kw)
def inspect():return json.loads(run(['docker','inspect',NAME]))[0]
def sha(data):return hashlib.sha256(data).hexdigest()
def source_hash(name):return run(['docker','exec',NAME,'python','-c',"import hashlib;print(hashlib.sha256(open('/app/service/"+name+"','rb').read()).hexdigest())"]).strip()
def same(old,new):
 for field in ['Cmd','Entrypoint','User','WorkingDir','Healthcheck','StopSignal','StopTimeout']:
  assert old['Config'].get(field)==new['Config'].get(field),'Config changed: '+field
 def env(d):
  rows=d['Config'].get('Env') or [];values=dict(x.split('=',1) for x in rows);assert len(rows)==len(values);return values
 assert env(old)==env(new),'Environment changed'
 for field in ['Memory','NanoCpus','CpuShares','MemoryReservation','MemorySwap','PortBindings','RestartPolicy','ReadonlyRootfs','Privileged','CapAdd','CapDrop','SecurityOpt']:
  assert old['HostConfig'].get(field)==new['HostConfig'].get(field),'Host config changed: '+field
 assert sorted(old['HostConfig'].get('Binds') or [])==sorted(new['HostConfig'].get('Binds') or [])
 assert sorted((m['Source'],m['Destination'],m['RW']) for m in old['Mounts'])==sorted((m['Source'],m['Destination'],m['RW']) for m in new['Mounts'])
def save(config):
 tmp=manifest.with_suffix('.bulk.tmp');fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 with os.fdopen(fd,'w') as f:f.write(json.dumps(config,indent=2)+'\n');f.flush();os.fsync(f.fileno())
 os.replace(tmp,manifest)
def compose():return subprocess.run(['docker','compose','-p','twin-onlyfans','-f',str(manifest),'up','-d','--no-deps','--timeout','180','api'],check=True,capture_output=True,text=True,timeout=220)
old=json.loads((ROOT/'before.json').read_text());live=inspect()
assert live['Image']==prepared['before_image'];same(old,live)
assert sha(manifest.read_bytes())==sha((ROOT/'compose.before.json').read_bytes()),'Concurrent manifest change'
for name,hashes in prepared['hashes'].items():
 if 'before' in hashes:assert source_hash(name)==hashes['before'],'Concurrent source change'
assert not [line for line in run(['docker','diff',NAME]).splitlines() if line.endswith('.py')],'Uncommitted live Python'
# The exact running image is retained for rollback even if a later dashboard workflow prunes unused images.
with (ROOT/'api-rollback-image.tar').open('wb') as f:subprocess.run(['docker','save',prepared['before_image']],stdout=f,check=True)
changed=False
try:
 config=json.loads(manifest.read_text());config['services']['api']['image']=prepared['candidate_image'];config['services']['api']['command']=old['Config']['Cmd'];save(config);changed=True
 result=compose();(ROOT/'cutover.log').write_text(result.stdout+result.stderr)
 cmd=old['Config']['Cmd'];port=int(cmd[cmd.index('--port')+1]) if '--port' in cmd else 8000
 health_code='''import os,json,urllib.request,urllib.error
from onlyfans_read.contract import SCHEMA_VERSION,SCHEMA_VERSION_HEADER
from onlyfans_commands.settings import HEADER,SCHEMA
base='http://127.0.0.1:__PORT__'
for path in ['/healthz','/onlyfans/api/v1/ready']:
 headers={} if path=='/healthz' else {'Authorization':'Bearer '+os.environ['ONLYFANS_API_TOKEN'],SCHEMA_VERSION_HEADER:SCHEMA_VERSION}
 with urllib.request.urlopen(urllib.request.Request(base+path,headers=headers),timeout=15) as r:
  body=json.load(r);assert r.status==200 and body.get('ok') is True
  print(json.dumps({'endpoint':path,'status':r.status,'ok':True}))
# Authenticated empty batch must reach the new route and be rejected before any database write.
headers={'Authorization':'Bearer '+os.environ['ONLYFANS_CONTROL_TOKEN'],HEADER:SCHEMA,'Content-Type':'application/json'}
request=urllib.request.Request(base+'/onlyfans/control/v1/accounts/of-323358988/bulk-conversations',headers=headers,data=json.dumps({'action':'resume_automation','items':[],'commandKey':'a'*64,'actor':'op-session:'+'a'*64}).encode())
try:urllib.request.urlopen(request,timeout=15);raise AssertionError('Empty batch accepted')
except urllib.error.HTTPError as e:
 body=json.load(e);assert e.code==400 and body.get('code')=='invalid_request',body
 print(json.dumps({'bulk_route':True,'invalid_batch_rejected':True,'writes':0}))
'''.replace('__PORT__',str(port))
 deadline=time.monotonic()+70
 while True:
  try:
   health=run(['docker','exec','-e','PYTHONPATH=/app/service',NAME,'python','-c',health_code],stderr=subprocess.STDOUT);break
  except subprocess.CalledProcessError:
   assert time.monotonic()<deadline,'API readiness failed';time.sleep(2)
 live=inspect();assert live['Image']==prepared['candidate_image'] and live['State']['Running'] and live['RestartCount']==0;same(old,live)
 for name,hashes in prepared['hashes'].items():assert source_hash(name)==hashes['after']
 # The earlier free/paid photo repair must remain byte-identical.
 assert source_hash('onlyfans_runtime/text_delivery.py')=='813552a2e5f053cdc2df0abf6bbebc2ecfc6ae6f75245158eeb1edf70286fa26'
 record={'image':live['Image'],'source_commit':'cb937394e0c9bd914b499080d5913b9b0342c5f3','running':True,'configuration_preserved':True,'paid_photo_repair_preserved':True,'health':health.splitlines()}
 (ROOT/'deployed.json').write_text(json.dumps(record,indent=2));print(json.dumps(record));print('API_CUTOVER_VERIFIED_NO_FAN_CONTROLS_CHANGED')
except Exception:
 if changed:
  config=json.loads(manifest.read_text());current=inspect()
  if config['services']['api'].get('image')==prepared['candidate_image'] and current['Image'] in [prepared['before_image'],prepared['candidate_image']]:
   config['services']['api']['image']=prepared['before_image'];config['services']['api']['command']=old['Config']['Cmd'];save(config);compose();print('API_ROLLED_BACK')
 raise
