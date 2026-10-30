python3 - <<'PY'
import json,subprocess,pathlib,http.client,socket,time,urllib.request
root=pathlib.Path('/var/lib/twin-release/settings-compat-20261030')
def inspect(n):return json.loads(subprocess.check_output(['docker','inspect',n]))[0]
d=inspect('next-app-of');new=inspect('next-app')
assert d['Image']=='sha256:60c4edf7947e3af3cbe33454b2df90db508d58572a7a40bf3d5698ec2a33d1fb'
assert dict(e.split('=',1) for e in new['Config']['Env'])['GIT_SHA']=='8cde4384ab5f8af5e45a838d1d8c81ff41d9a245'
assert not d['Mounts']
check="const fs=require('node:fs');process.exit(fs.readdirSync('/tmp').some(x=>x.startsWith('creator-upload-'))?3:0)"
assert subprocess.run(['docker','exec','next-app-of','node','-e',check],capture_output=True).returncode==0,'Uploads active; no swap'
p=root/'next-app-of.promotion-before.json';assert not p.exists();p.write_text(json.dumps(d));p.chmod(0o600)
config=dict(d['Config']);config.update(Image=new['Image'],Hostname='next-app-of')
config['Env']=[x for x in config['Env'] if not x.startswith('GIT_SHA=')]+['GIT_SHA=8cde4384ab5f8af5e45a838d1d8c81ff41d9a245']
payload={**config,'HostConfig':d['HostConfig'],'NetworkingConfig':{'EndpointsConfig':{n:{'Aliases':v.get('Aliases') or ['next-app-of']} for n,v in d['NetworkSettings']['Networks'].items()}}}
class Docker(http.client.HTTPConnection):
 def __init__(self):super().__init__('localhost')
 def connect(self):
  self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.sock.connect('/var/run/docker.sock')
def api(method,path,body=None):
 c=Docker();c.request(method,'/v1.45'+path,body=json.dumps(body) if body is not None else None,headers={'Content-Type':'application/json'});r=c.getresponse();raw=r.read();assert r.status<300,str(r.status);return json.loads(raw) if raw else {}
backup='next-app-of-settings-compat-rollback-2e4dfea7'
subprocess.run(['docker','stop','--time','30','next-app-of'],check=True,stdout=subprocess.DEVNULL)
subprocess.run(['docker','rename','next-app-of',backup],check=True)
try:
 created=api('POST','/containers/create?name=next-app-of',payload);api('POST','/containers/'+created['Id']+'/start')
 ok=False
 for i in range(30):
  try:
   with urllib.request.urlopen('http://127.0.0.1:3002/twin',timeout=2) as response:assert response.status==200
   ok=True;break
  except Exception:time.sleep(1)
 assert ok,'Login health'
except Exception:
 subprocess.run(['docker','stop','--time','10','next-app-of'],stdout=subprocess.DEVNULL)
 subprocess.run(['docker','rename','next-app-of','next-app-of-settings-compat-failed'])
 subprocess.run(['docker','rename',backup,'next-app-of'],check=True);subprocess.run(['docker','start','next-app-of'],check=True);raise
v=inspect('next-app-of');assert v['Image']==new['Image'] and v['State']['Running']
result={'container':'next-app-of','source':'8cde4384ab5f8af5e45a838d1d8c81ff41d9a245','image':v['Image'],'health':'passed','rollbackContainer':backup}
(root/'next-app-of.compat-result.json').write_text(json.dumps(result));print(json.dumps(result))
PY