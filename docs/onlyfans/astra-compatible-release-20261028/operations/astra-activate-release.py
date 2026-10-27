import pathlib,json,subprocess,os,hashlib,datetime,fcntl,urllib.request,time
R=pathlib.Path('/var/lib/twin-release/astra-compatible-20261028');P=R/'cutover-private'
IMAGE='sha256:b4c3ec13342111c1eadc75ad224ab314b659ff60494d78513b37dbdeb67e33ec'
def run(a,timeout=120,input=None):
 p=subprocess.run(a,text=True,input=input,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout)
 if p.returncode:
  (R/'astra-activate-release.py.error.private.log').write_text(p.stdout+'\n'+p.stderr)
  print(json.dumps({'operation':'astra-activate-release.py','exit':p.returncode,'error':p.stderr[-1500:]}),flush=True)
  raise RuntimeError('Release operation failed')
 return p.stdout.strip()
lock=(R/'cutover.lock').open('w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
assert not(R/'activation-proof.json').exists(),'Activation already recorded'
assert json.loads((R/'approved-validation.json').read_text())['aggregateAccepted'] is True
smoke=json.loads((R/'production-session-smoke.json').read_text())
assert smoke['logoutAction'] and smoke['otherSessionPreserved'] and smoke['legacyCompatibility'] is False
assert smoke['source']=='4599999761dbf2ff2b517359642d10262324867a'
proof=json.loads((R/'read-roles-proof.json').read_text())
assert proof['paidReady'] is True,'Financial evidence is not ready'
assert (datetime.datetime.now(datetime.timezone.utc)-datetime.datetime.fromisoformat(proof['at'])).total_seconds()<180,'Refresh financial readiness before activation'
main=json.loads((P/'main.forward.json').read_text())
assert hashlib.sha256(pathlib.Path('/etc/twin-onlyfans/media-agency-release-compose.json').read_bytes()).hexdigest()==hashlib.sha256((P/'main.forward.json').read_bytes()).hexdigest()
sender=main['services']['fastt-supervisor']
old=json.loads((P/'twin-onlyfans-fastt-supervisor-1.json').read_text())
oldenv=dict(v.split('=',1) for v in old['Config']['Env'] if '=' in v)
assert sender['environment']==oldenv and sender['image']==IMAGE
assert oldenv['FASTT_FAN_DAILY_REPLY_LIMIT']=='2000'and oldenv['ONLYFANS_FASTT_SCOPE']=='db:*'
assert main['services']['fastt-runner']['profiles']==['ops']
# Restore the original command capabilities, then resume only the existing persistent sender.
run(['docker','compose','-p','twin-onlyfans','-f','/etc/twin-onlyfans/media-agency-release-compose.json','up','-d','--no-deps','--no-build','--pull','never','api','fastt-supervisor'],timeout=150)
code="import pathlib,hashlib,json;p=pathlib.Path('/app/service');print(json.dumps({str(f.relative_to(p)):hashlib.sha256(f.read_bytes()).hexdigest()for f in p.rglob('*.py')}))"
roles=[]
for name in ['twin-onlyfans-api-1','twin-onlyfans-fastt-supervisor-1']:
 d=json.loads(run(['docker','inspect',name]))[0]
 assert d['Image']==IMAGE and d['State']['Running']and not d['State']['Restarting']and not d['State']['OOMKilled']
 assert json.loads(run(['docker','exec',name,'python','-c',code]))==proof['sourceFiles']
 actual=dict(v.split('=',1)for v in d['Config']['Env']if'='in v)
 original=json.loads((P/(name+'.json')).read_text())
 expected=dict(v.split('=',1)for v in original['Config']['Env']if'='in v)
 assert actual==expected,'Environment changed: '+name
 roles.append({'name':name,'image':d['Image'],'container':d['Id'],'verifiedPythonFiles':len(proof['sourceFiles']),'restartCount':d['RestartCount']})
token=dict(v.split('=',1)for v in json.loads((P/'twin-onlyfans-api-1.json').read_text())['Config']['Env']if'='in v)['ONLYFANS_API_TOKEN']
for i in range(20):
 try:
  req=urllib.request.Request('http://127.0.0.1:8788/onlyfans/api/v1/ready',headers={'Authorization':'Bearer '+token,'X-OnlyFans-Schema-Version':'of-preview.v2'})
  with urllib.request.urlopen(req,timeout=5)as response:assert response.status==200;ready=json.load(response)
  break
 except Exception:
  if i==19:raise
  time.sleep(1)
report={'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'backend':'293b23dbdf8dd5e97f27d6c0bef8ef4710415c06','dashboard':'4599999761dbf2ff2b517359642d10262324867a','schema':'0097_release_controls','roles':roles,'ready':ready,'restoredExistingAutomation':True,'manualFanTestsSent':False,'accountControlsChanged':False,'dailyLimit':2000}
(R/'activation-proof.json').write_text(json.dumps(report,indent=2));print(json.dumps(report))
