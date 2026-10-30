"""Promote the already-tested, already-serving primary dashboard image to its OF companion."""
import json,pathlib,subprocess,time,os,urllib.request,urllib.error
ROOT=pathlib.Path('/var/lib/twin-release/bulk-ai-20261030')
SHA='2f98dd5a1f629db17e14bdf03cd960382d9cf432'
NAME='next-app-of'
def run(args,**kw):return subprocess.check_output(args,text=True,**kw)
def inspect(name):return json.loads(run(['docker','inspect',name]))[0]
def env(d):return dict(x.split('=',1) for x in d['Config'].get('Env',[]))
primary=inspect('next-app');old=inspect(NAME)
assert primary['State']['Running'] and env(primary).get('GIT_SHA')==SHA,'Primary exact release is not deployed'
assert old['Image']=='sha256:724e2615043dd4b1f6a9ad2a20c6f44d5bad9887277d4d24c395aaf3bbce93f2','Companion changed concurrently'
assert old['Config']['Cmd']==['node','server.js'] and old['Config']['Entrypoint']==['docker-entrypoint.sh']
assert not old['Mounts'] and list(old['NetworkSettings']['Networks'])==['twin-onlyfans_default']
assert old['HostConfig']['PortBindings']=={'3002/tcp':[{'HostIp':'127.0.0.1','HostPort':'3002'}]}
assert not old['HostConfig'].get('Privileged') and not old['HostConfig'].get('CapAdd')
previous=NAME+'-before-bulk-20261030'
assert subprocess.run(['docker','inspect',previous],capture_output=True).returncode!=0,'Rollback container already exists'
# Protected snapshot is used without printing or copying any credential values elsewhere.
snapshot=ROOT/'companion.cutover-before.json';snapshot.write_text(json.dumps(old));snapshot.chmod(0o600)
values=env(old);values['GIT_SHA']=SHA
assert len(old['Config']['Env'])==len(values),'Duplicate environment keys'
assert not any('\n' in key or '\n' in value for key,value in values.items()),'Multiline environment requires a different recreation'
envfile=ROOT/'companion.runtime.env';envfile.write_text(''.join(k+'='+v+'\n' for k,v in values.items()));envfile.chmod(0o600)
h=old['HostConfig']
args=['docker','run','-d','--name',NAME,'--network','twin-onlyfans_default','-p','127.0.0.1:3002:3002','--env-file',str(envfile),'--restart',h['RestartPolicy']['Name'],'--stop-timeout',str(old['Config'].get('StopTimeout',30))]
for flag,field in [('--memory','Memory'),('--memory-reservation','MemoryReservation'),('--memory-swap','MemorySwap'),('--cpu-shares','CpuShares')]:
 if h.get(field):args.extend([flag,str(h[field])])
if h.get('NanoCpus'):args.extend(['--cpus',str(h['NanoCpus']/1e9)])
if h.get('ReadonlyRootfs'):args.append('--read-only')
for value in h.get('SecurityOpt') or []:args.extend(['--security-opt',value])
for value in h.get('CapDrop') or []:args.extend(['--cap-drop',value])
if h.get('LogConfig',{}).get('Type'):
 args.extend(['--log-driver',h['LogConfig']['Type']])
 for key,value in h['LogConfig'].get('Config',{}).items():args.extend(['--log-opt',key+'='+value])
args.append(primary['Image'])
changed=False
try:
 assert inspect(NAME)['Id']==old['Id'] and inspect('next-app')['Image']==primary['Image']
 run(['docker','stop','-t','30',NAME]);run(['docker','rename',NAME,previous]);changed=True
 run(args)
 deadline=time.monotonic()+90
 while True:
  try:
   with urllib.request.urlopen('http://127.0.0.1:3002/',timeout=5) as response:assert response.status==200
   break
  except Exception:
   assert time.monotonic()<deadline,'Companion failed health';time.sleep(2)
 current=inspect(NAME)
 assert current['Image']==primary['Image'] and current['State']['Running'] and current['RestartCount']==0
 assert env(current)==values
 for field in ['Cmd','Entrypoint','User','WorkingDir','Healthcheck','StopSignal','StopTimeout']:
  assert current['Config'].get(field)==old['Config'].get(field),'Config drift: '+field
 for field in ['Memory','MemoryReservation','MemorySwap','NanoCpus','CpuShares','RestartPolicy','PortBindings','Privileged','ReadonlyRootfs','SecurityOpt','CapAdd','CapDrop','LogConfig']:
  assert current['HostConfig'].get(field)==h.get(field),'Host drift: '+field
 assert current['Mounts']==old['Mounts']
 evidence={'primary_image':primary['Image'],'companion_image':current['Image'],'sha':SHA,'companion_running':True,'environment_preserved':True,'host_configuration_preserved':True,'rollback_container':previous}
 (ROOT/'dashboard-companion.json').write_text(json.dumps(evidence,indent=2));print(json.dumps(evidence))
except Exception:
 if changed:
  subprocess.run(['docker','rm','-f',NAME],capture_output=True)
  run(['docker','rename',previous,NAME]);run(['docker','start',NAME]);print('COMPANION_ROLLED_BACK')
 raise
finally:envfile.unlink(missing_ok=True)
