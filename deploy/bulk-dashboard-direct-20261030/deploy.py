"""Deploy an independently built dashboard image; retain both rollback containers."""
import json,pathlib,subprocess,time,os,urllib.request,fcntl
ROOT=pathlib.Path('/var/lib/twin-release/bulk-dashboard-direct-2f98dd5');ROOT.mkdir(mode=0o700,exist_ok=True)
SHA='2f98dd5a1f629db17e14bdf03cd960382d9cf432'
OLD='sha256:724e2615043dd4b1f6a9ad2a20c6f44d5bad9887277d4d24c395aaf3bbce93f2'
IMAGE=RELEASE['image']
assert RELEASE['buildStatus']=='SUCCEEDED' and IMAGE.startswith('323149985112.dkr.ecr.ap-southeast-2.amazonaws.com/twin-onlyfans@sha256:')
lock=open(ROOT/'deploy.lock','a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
def run(args,**kw):return subprocess.check_output(args,text=True,**kw)
def inspect(name):return json.loads(run(['docker','inspect',name]))[0]
def env(d):
 rows=d['Config'].get('Env',[]);values=dict(x.split('=',1) for x in rows);assert len(rows)==len(values);return values
old={name:inspect(name) for name in ['next-app','next-app-of']}
for name,d in old.items():
 assert d['Image']==OLD and env(d).get('GIT_SHA')=='8cde4384ab5f8af5e45a838d1d8c81ff41d9a245','Concurrent dashboard change'
 assert not d['Mounts'] and list(d['NetworkSettings']['Networks'])==['twin-onlyfans_default']
 assert d['Config']['Cmd']==['node','server.js'] and d['Config']['Entrypoint']==['docker-entrypoint.sh']
 p=ROOT/(name+'.before.json');p.write_text(json.dumps(d));p.chmod(0o600)
 assert subprocess.run(['docker','inspect',name+'-rollback-2f98dd5'],capture_output=True).returncode!=0
api=inspect('twin-onlyfans-api-1');assert api['Image']=='sha256:87b580750f3bc20a2ba3305c021357f9e6f957127ac79c6a6b9b215dc656547f'
# Existing instance identity retrieves this single image. No credentials are printed.
login=subprocess.Popen(['aws','ecr','get-login-password','--region','ap-southeast-2'],stdout=subprocess.PIPE)
subprocess.run(['docker','login','--username','AWS','--password-stdin','323149985112.dkr.ecr.ap-southeast-2.amazonaws.com'],stdin=login.stdout,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True);login.stdout.close();assert login.wait()==0
with (ROOT/'pull.log').open('w') as log:subprocess.run(['docker','pull',IMAGE],stdout=log,stderr=subprocess.STDOUT,check=True)
image=inspect(IMAGE)['Id']
check="""const fs=require('fs');const assert=require('assert');const p=JSON.parse(fs.readFileSync('.next/server/app-paths-manifest.json'));assert(p['/api/onlyfans-controls/accounts/[accountId]/bulk/route']);let found=false;function walk(d){for(const f of fs.readdirSync(d,{withFileTypes:true})){let path=d+'/'+f.name;if(f.isDirectory())walk(path);else if(f.name.endsWith('.js')){const s=fs.readFileSync(path,'utf8');if(s.includes('Disable AI replies for existing chats')&&s.includes('Enable AI replies for existing chats'))found=true}}}walk('.next/static');assert(found,'Both buttons absent from browser bundle');assert.equal(process.env.GIT_SHA,'2f98dd5a1f629db17e14bdf03cd960382d9cf432');console.log('BUILT_ROUTE_AND_BUTTONS_VERIFIED')"""
print(run(['docker','run','--rm','--network','none','--entrypoint','node',IMAGE,'-e',check]).strip(),flush=True)
def preserved(before,after):
 expected=env(before);expected['GIT_SHA']=SHA;assert env(after)==expected
 for field in ['Cmd','Entrypoint','User','WorkingDir','Healthcheck','StopSignal','StopTimeout']:assert before['Config'].get(field)==after['Config'].get(field),'Config drift '+field
 for field in ['Memory','MemoryReservation','MemorySwap','NanoCpus','CpuShares','RestartPolicy','PortBindings','Privileged','ReadonlyRootfs','SecurityOpt','CapAdd','CapDrop','LogConfig']:assert before['HostConfig'].get(field)==after['HostConfig'].get(field),'Host drift '+field
 assert after['Mounts']==before['Mounts']
def http(port):
 with urllib.request.urlopen('http://127.0.0.1:'+str(port)+'/',timeout=10) as r:assert r.status==200
changed=[];report=[]
try:
 for name,port in [('next-app-of',3002),('next-app',3001)]:
  d=old[name];assert inspect(name)['Id']==d['Id'],'Concurrent recreation'
  values=env(d);values['GIT_SHA']=SHA;assert not any('\n' in v for v in values.values())
  file=ROOT/(name+'.env');file.write_text(''.join(k+'='+v+'\n' for k,v in values.items()));file.chmod(0o600)
  h=d['HostConfig'];assert h['PortBindings']=={str(port)+'/tcp':[{'HostIp':'127.0.0.1','HostPort':str(port)}]}
  args=['docker','run','-d','--name',name,'--network','twin-onlyfans_default','-p',f'127.0.0.1:{port}:{port}','--env-file',str(file),'--restart',h['RestartPolicy']['Name'],'--stop-timeout',str(d['Config'].get('StopTimeout',30))]
  for flag,field in [('--memory','Memory'),('--memory-reservation','MemoryReservation'),('--memory-swap','MemorySwap'),('--cpu-shares','CpuShares')]:
   if h.get(field):args.extend([flag,str(h[field])])
  if h.get('NanoCpus'):args.extend(['--cpus',str(h['NanoCpus']/1e9)])
  if h.get('ReadonlyRootfs'):args.append('--read-only')
  assert not h.get('Privileged') and not h.get('CapAdd')
  for value in h.get('SecurityOpt') or []:args.extend(['--security-opt',value])
  for value in h.get('CapDrop') or []:args.extend(['--cap-drop',value])
  if h.get('LogConfig',{}).get('Type'):
   args.extend(['--log-driver',h['LogConfig']['Type']])
   for k,v in h['LogConfig'].get('Config',{}).items():args.extend(['--log-opt',k+'='+v])
  args.append(IMAGE)
  idlecheck="const fs=require('fs');const n=fs.readdirSync('/tmp').filter(x=>x.startsWith('creator-upload-')).length;process.exit(n?3:0)"
  idle_deadline=time.monotonic()+600
  while True:
   idle=subprocess.run(['docker','exec',name,'node','-e',idlecheck],capture_output=True)
   if idle.returncode==0:break
   assert idle.returncode==3 and time.monotonic()<idle_deadline,'Cannot confirm creator upload idle'
   time.sleep(10)
  print('CREATOR_UPLOAD_IDLE '+name,flush=True)
  run(['docker','stop','-t','30',name]);run(['docker','rename',name,name+'-rollback-2f98dd5']);changed.append(name)
  try:run(args)
  finally:file.unlink(missing_ok=True)
  deadline=time.monotonic()+90
  while True:
   try:http(port);break
   except Exception:
    assert time.monotonic()<deadline,'Dashboard readiness failed';time.sleep(2)
  current=inspect(name);assert current['Image']==image and current['State']['Running'] and current['RestartCount']==0;preserved(d,current)
  print(run(['docker','exec',name,'node','-e',check]).strip(),flush=True)
  # The live new route must deny an unauthenticated write, with no fan changes.
  routecheck="""(async()=>{const r=await fetch('http://127.0.0.1:'+process.env.PORT+'/api/onlyfans-controls/accounts/of-323358988/bulk',{method:'POST',redirect:'manual',headers:{'content-type':'application/json'},body:'{}'});if(![401,403,307,302,303].includes(r.status))throw Error('Unexpected route authentication '+r.status);console.log('LIVE_BULK_AUTH_GUARD',r.status)})().catch(e=>{console.error(e.message);process.exitCode=1})"""
  print(run(['docker','exec',name,'node','-e',routecheck]).strip(),flush=True)
  backendcheck="""(async()=>{const token=process.env.ONLYFANS_CONTROL_TOKEN;const base=process.env.ONLYFANS_API_BASE_URL;if(!token||!base)throw Error('Missing bulk backend configuration');const r=await fetch(base.replace(/\\/+$/,'')+'/onlyfans/control/v1/accounts/of-323358988/bulk-conversations',{method:'POST',headers:{Authorization:'Bearer '+token,'X-OnlyFans-Control-Version':'of-controls.v1','Content-Type':'application/json'},body:JSON.stringify({action:'resume_automation',items:[],commandKey:'a'.repeat(64),actor:'op-session:'+'a'.repeat(64)}),signal:AbortSignal.timeout(15000)});const data=await r.json();if(r.status!==400||data.code!=='invalid_request')throw Error('Bulk API configuration check failed: '+r.status);console.log('LIVE_BULK_API_CONNECTED_EMPTY_REQUEST_REFUSED_NO_WRITES')})().catch(e=>{console.error(e.message);process.exitCode=1})"""
  print(run(['docker','exec',name,'node','-e',backendcheck]).strip(),flush=True)
  record={'name':name,'image':image,'sha':SHA,'http':200,'environment_preserved':True,'configuration_preserved':True,'restartCount':0};report.append(record);print(json.dumps(record),flush=True)
 with (ROOT/'dashboard-recovery.log').open('w') as log:
  subprocess.run(['docker','exec','next-app','node','dashboard-recovery-check.cjs'],stdout=log,stderr=subprocess.STDOUT,check=True,timeout=240)
 print('LIVE_DASHBOARD_EARNINGS_AND_PRIVATE_MEDIA_CHECK_PASSED',flush=True)
 assert inspect('twin-onlyfans-api-1')['Id']==api['Id'],'API changed during dashboard release'
 (ROOT/'deployed.json').write_text(json.dumps({'release':RELEASE,'dashboards':report,'fan_changes':0},indent=2));print('DIRECT_DASHBOARD_DEPLOY_VERIFIED',flush=True)
except Exception:
 for name in reversed(changed):
  subprocess.run(['docker','rm','-f',name],capture_output=True);run(['docker','rename',name+'-rollback-2f98dd5',name]);run(['docker','start',name]);print('ROLLED_BACK '+name,flush=True)
 raise
