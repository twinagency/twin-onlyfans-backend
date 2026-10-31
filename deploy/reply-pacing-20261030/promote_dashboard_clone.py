"""Promote the already-pipeline-tested dashboard image to the existing OF consumer."""
import http.client,json,os,pathlib,socket,subprocess,time,urllib.request
ROOT=pathlib.Path('/var/lib/twin-release/reply-pacing-20261030-208785')
SOURCE='2e4dfea7acbc5fa3fe14b64dc2d3bb981b5da7db'
NAME='next-app-of';BACKUP='next-app-of-reply-rollback-208785'
def inspect(name):return json.loads(subprocess.check_output(['docker','inspect',name]))[0]
def run(args):return subprocess.run(args,check=True,capture_output=True,text=True)
class Docker(http.client.HTTPConnection):
 def __init__(self):super().__init__('localhost')
 def connect(self):self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.sock.connect('/var/run/docker.sock')
def request(method,path,body=None):
 c=Docker();c.request(method,'/v1.45'+path,body=json.dumps(body) if body is not None else None,headers={'Content-Type':'application/json'});r=c.getresponse();data=r.read();c.close()
 assert 200<=r.status<300,'Docker API refused operation: '+str(r.status)
 return json.loads(data) if data else None
def private(path,value):path.write_text(json.dumps(value,indent=2));path.chmod(0o600)
def env(config):return dict(x.split('=',1) for x in config['Env'])
def main():
 os.umask(0o077);ROOT.mkdir(parents=True,exist_ok=True)
 new=inspect('next-app');old=inspect(NAME)
 assert new['State']['Running'] and env(new['Config']).get('GIT_SHA')==SOURCE,'Main dashboard not the candidate'
 if old['Image']==new['Image']:
  print(json.dumps({'already_deployed':True,'image':new['Image']}));return
 assert old['Image']=='sha256:99c570f226a2bebd435af40cee5ef50072d30ffb5b95e3a3eb26fdd7aa880fc1','OF dashboard baseline drift'
 assert subprocess.run(['docker','inspect',BACKUP],capture_output=True).returncode!=0,'Review previous backup first'
 private(ROOT/'dashboard-of.before.json',old)
 body=dict(old['Config']);body['Image']=new['Image']
 body['Env']=[('GIT_SHA='+SOURCE) if x.startswith('GIT_SHA=') else x for x in body['Env']]
 body['HostConfig']=old['HostConfig']
 body['NetworkingConfig']={'EndpointsConfig':{network:{'Aliases':[a for a in (data.get('Aliases') or []) if a not in (old['Id'],old['Id'][:12])]} for network,data in old['NetworkSettings']['Networks'].items()}}
 stopped=renamed=created=False
 try:
  run(['docker','stop','-t','30',NAME]);stopped=True
  run(['docker','rename',NAME,BACKUP]);renamed=True
  request('POST','/containers/create?name='+NAME,body);created=True
  request('POST','/containers/'+NAME+'/start')
  ready=False
  for _ in range(40):
   try:
    with urllib.request.urlopen('http://127.0.0.1:3002/',timeout=3) as response:ready=response.status==200
    if ready:break
   except Exception:pass
   time.sleep(1)
  assert ready,'OF dashboard HTTP readiness failed'
  actual=inspect(NAME);assert actual['Image']==new['Image'] and actual['State']['Running']
  assert {k:v for k,v in env(actual['Config']).items() if k!='GIT_SHA'}=={k:v for k,v in env(old['Config']).items() if k!='GIT_SHA'},'Environment drift'
  for key in ['Cmd','Entrypoint','User','WorkingDir']:assert actual['Config'].get(key)==old['Config'].get(key),key+' drift'
  assert actual['HostConfig']['PortBindings']==old['HostConfig']['PortBindings'],'Port binding drift'
  assert actual['HostConfig']['Binds']==old['HostConfig']['Binds'],'Mount drift'
  result={'deployed':True,'container':NAME,'source':SOURCE,'image':new['Image'],'runtime_preserved':True,'http_ready':True,'rollback_container':BACKUP}
  private(ROOT/'dashboard-of.result.json',result);print(json.dumps(result))
 except Exception:
  if created:subprocess.run(['docker','rm','-f',NAME],capture_output=True)
  if renamed:run(['docker','rename',BACKUP,NAME])
  if stopped:run(['docker','start',NAME])
  private(ROOT/'dashboard-of.result.json',{'deployed':False,'rollback_started':True})
  raise
if __name__=='__main__':main()
