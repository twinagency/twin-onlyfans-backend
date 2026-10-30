import json, pathlib, subprocess, hashlib, os, time, fcntl, shlex

ROOT=pathlib.Path('/var/lib/twin-release/manual-photo-20261030-164b0d6')
manifest=pathlib.Path('/etc/twin-onlyfans/media-agency-release-compose.json')
prepared=json.loads((ROOT/'prepared.json').read_text())
lock=open(ROOT/'cutover.lock','a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)

def run(args,**kw):return subprocess.check_output(args,text=True,**kw)
def inspect(name):return json.loads(run(['docker','inspect',name]))[0]
def saved(service):return json.loads((ROOT/(service+'.before.json')).read_text())
def sha(b):return hashlib.sha256(b).hexdigest()
def source_hash(name):
    return run(['docker','exec',name,'python','-c',"import hashlib;print(hashlib.sha256(open('/app/service/onlyfans_runtime/text_delivery.py','rb').read()).hexdigest())"]).strip()
def mounts(d):return sorted((m['Source'],m['Destination'],m['RW']) for m in d['Mounts'])
def config_preserved(old,new):
    def env(d):
        rows=d['Config'].get('Env') or []
        values=dict(x.split('=',1) for x in rows)
        assert len(values)==len(rows),'Duplicate environment keys'
        return values
    assert env(old)==env(new),'Runtime environment values changed'
    for field in ['Cmd','Entrypoint','User','WorkingDir','Healthcheck','StopSignal','StopTimeout']:
        assert old['Config'].get(field)==new['Config'].get(field),'Runtime config drift: '+field
    for field in ['Memory','NanoCpus','CpuShares','MemoryReservation','MemorySwap','Binds','PortBindings','RestartPolicy','ReadonlyRootfs','Privileged','CapAdd','CapDrop','SecurityOpt']:
        assert old['HostConfig'].get(field)==new['HostConfig'].get(field),'Host config drift: '+field
    assert mounts(old)==mounts(new),'Mount drift'

expected={s:d['before_image'] for s,d in prepared.items()}
expected_manifest_hash=sha((ROOT/'compose.reconciled.json').read_bytes())
assert sha(manifest.read_bytes())==expected_manifest_hash,'Manifest changed after preparation'
for service,record in prepared.items():
    live=inspect(record['container']);assert live['Image']==record['before_image'],'Concurrent image change: '+service
    config_preserved(saved(service),live)
    assert source_hash(record['container'])==record['before_source'],'Concurrent source change: '+service
    changes=run(['docker','diff',record['container']]).splitlines()
    assert not [x for x in changes if x.endswith('.py')], 'Uncommitted Python source in '+service

effective=json.loads(run(['docker','compose','-p','twin-onlyfans','-f',str(manifest),'config','--format','json']))
api_cmd=saved('api')['Config']['Cmd'];port=8000
if '--port' in api_cmd:port=int(api_cmd[api_cmd.index('--port')+1])
elif effective['services']['api'].get('ports'):port=int(effective['services']['api']['ports'][0]['target'])

def write_config(config):
    temp=manifest.with_name(manifest.name+'.manual-photo.tmp')
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f:
        f.write(json.dumps(config,indent=2)+'\n');f.flush();os.fsync(f.fileno())
    os.replace(temp,manifest)

def compose(service):
    return subprocess.run(['docker','compose','-p','twin-onlyfans','-f',str(manifest),'up','-d','--no-deps','--timeout','180',service],check=True,capture_output=True,text=True,timeout=220)

changed=[];report={}
try:
    for service in ['worker','fastt-supervisor','api']:
        assert sha(manifest.read_bytes())==expected_manifest_hash,'Concurrent manifest edit'
        for other,record in prepared.items():
            assert inspect(record['container'])['Image']==expected[other], 'Concurrent image change: '+other
        record=prepared[service];old=saved(service)
        config=json.loads(manifest.read_text())
        config['services'][service]['image']=record['candidate_image']
        # Match the observed live API invocation if the manifest lagged behind it.
        desired=effective['services'][service].get('command')
        if desired is None:
            parent=json.loads(run(['docker','image','inspect',record['before_image']]))[0]
            desired=parent['Config'].get('Cmd')
        elif isinstance(desired,str):desired=shlex.split(desired)
        if desired!=old['Config']['Cmd']:
            config['services'][service]['command']=old['Config']['Cmd']
        write_config(config);expected_manifest_hash=sha(manifest.read_bytes())
        # Mark before recreation so a startup failure can be rolled back too.
        changed.append(service)
        result=compose(service);(ROOT/(service+'.cutover.log')).write_text(result.stdout+result.stderr)
        expected[service]=record['candidate_image']
        deadline=time.monotonic()+70
        while True:
            live=inspect(record['container'])
            assert live['Image']==record['candidate_image'],'Unexpected deployed image'
            health=live['State'].get('Health',{}).get('Status')
            if live['State']['Running'] and health in [None,'healthy']:break
            assert time.monotonic()<deadline,'Service failed health check: '+service
            time.sleep(2)
        config_preserved(old,live)
        assert source_hash(record['container'])==record['after_source'],'Deployed source mismatch'
        smoke=run(['docker','run','--rm','--network','none','--entrypoint','python','-e','PYTHONPATH=/app/service','-e','PYTHON_DOTENV_DISABLED=1','--mount','type=bind,src='+str(ROOT/'smoke.py')+',dst=/tmp/manual-photo-smoke.py,readonly',live['Image'],'/tmp/manual-photo-smoke.py'])
        report[service]={'image':live['Image'],'id':live['Id'],'source':record['after_source'],'state':live['State']['Status'],'health':health,'offline':json.loads(smoke),'configuration_preserved':True}
        print(json.dumps({'deployed':service,**report[service]}),flush=True)
    health_code='''import os,json,urllib.request
from onlyfans_read.contract import SCHEMA_VERSION,SCHEMA_VERSION_HEADER
base='http://127.0.0.1:__PORT__'
for path in ['/healthz','/onlyfans/api/v1/ready']:
 headers={} if path=='/healthz' else {'Authorization':'Bearer '+os.environ['ONLYFANS_API_TOKEN'],SCHEMA_VERSION_HEADER:SCHEMA_VERSION}
 with urllib.request.urlopen(urllib.request.Request(base+path,headers=headers),timeout=15) as r:
  data=json.load(r);assert r.status==200 and data.get('ok') is True
  print(json.dumps({'endpoint':path,'status':r.status,'ok':data.get('ok'),'schemaReady':data.get('schemaReady')}))
'''.replace('__PORT__',str(port))
    deadline=time.monotonic()+45
    while True:
        try:
            health=run(['docker','exec','-e','PYTHONPATH=/app/service',prepared['api']['container'],'python','-c',health_code],stderr=subprocess.STDOUT)
            break
        except subprocess.CalledProcessError:
            assert time.monotonic()<deadline,'API readiness did not recover'
            time.sleep(2)
    print(health,flush=True)
    time.sleep(8)
    for service,record in prepared.items():
        live=inspect(record['container']);assert live['Image']==record['candidate_image'] and live['State']['Running']
        assert live['RestartCount']==0,'Service restarted unexpectedly: '+service
        config_preserved(saved(service),live)
    (ROOT/'deployed.json').write_text(json.dumps(report,indent=2))
    (ROOT/'health.jsonl').write_text(health)
    print('CUTOVER_VERIFIED_NO_MESSAGES_SENT',flush=True)
except Exception as exc:
    print('CUTOVER_FAILED '+type(exc).__name__+': '+str(exc),flush=True)
    # Restore only this release's image keys; never overwrite unrelated edits.
    for service in reversed(changed):
        record=prepared[service]
        current=inspect(record['container'])
        if current['Image'] not in [record['before_image'],record['candidate_image']]:
            print('ROLLBACK_SKIPPED_CONCURRENT_IMAGE '+service,flush=True);continue
        config=json.loads(manifest.read_text())
        if config['services'][service].get('image')!=record['candidate_image']:
            print('ROLLBACK_SKIPPED_CONCURRENT_MANIFEST '+service,flush=True);continue
        config['services'][service]['image']=record['before_image']
        config['services'][service]['command']=saved(service)['Config']['Cmd']
        write_config(config);compose(service)
        print('ROLLED_BACK '+service,flush=True)
    raise
