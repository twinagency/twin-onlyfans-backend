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
assert (R/'existing-automation-scope-proof.json').exists()
assert (R/'production-backup/remote-retention-proof.json').exists()

main=json.loads((P/'main.forward.json').read_text())
assert hashlib.sha256(pathlib.Path('/etc/twin-onlyfans/media-agency-release-compose.json').read_bytes()).hexdigest()==hashlib.sha256((P/'main.forward.json').read_bytes()).hexdigest()
sender=main['services']['fastt-supervisor']
old=json.loads((P/'twin-onlyfans-fastt-supervisor-1.json').read_text())
oldenv=dict(v.split('=',1) for v in old['Config']['Env'] if '=' in v)
assert sender['environment']==oldenv and sender['image']==IMAGE
assert oldenv['FASTT_FAN_DAILY_REPLY_LIMIT']=='2000'and oldenv['ONLYFANS_FASTT_SCOPE']=='db:*'
assert main['services']['fastt-runner']['profiles']==['ops']

# Restore only pre-existing automated chats on the two accounts with no current
# unlinked purchases. This is a restricted operational resumption, not paid
# ledger readiness or public-launch approval. All runtime guards remain intact.
scope_code=r'''
import os,json
from sqlalchemy import create_engine,text
e=create_engine(os.environ['DATABASE_URL'],connect_args={'options':'-c default_transaction_read_only=on -c statement_timeout=10000'})
q="""SELECT c.account_id,c.fan_id FROM of_conversation_state c JOIN of_runtime r ON r.account_id=c.account_id WHERE r.enabled AND c.mode='automated' AND c.takeover_state='none' AND c.account_id IN ('of-176774528','of-99421145') ORDER BY c.account_id,c.fan_id"""
with e.connect()as c:
 rows=[dict(x)for x in c.execute(text(q)).mappings()]
 bad=c.scalar(text("""SELECT count(*) FROM transactions t JOIN of_runtime r ON r.account_id=t.account_id JOIN of_creator_bindings b ON b.id=r.binding_id WHERE t.account_id IN ('of-176774528','of-99421145') AND t.source='ledger' AND t.kind='ppv_message' AND t.message_id IS NULL AND t.status IN ('pending','cleared','refund_pending') AND t.occurred_at>=b.created_at"""))
 assert bad==0 and len(rows)==8 and {x['account_id']for x in rows}=={'of-176774528','of-99421145'}
 print(json.dumps(rows))
'''
rows=json.loads(run(['docker','exec','twin-onlyfans-worker-1','python','-c',scope_code]))
restricted_scope=','.join(x['account_id'][3:]+':'+str(x['fan_id']) for x in rows)
sender['environment']=dict(oldenv,ONLYFANS_FASTT_SCOPE=restricted_scope)
canonical=pathlib.Path('/etc/twin-onlyfans/media-agency-release-compose.json')
stage=canonical.with_suffix('.astra-scoped-new')
stage.write_text(json.dumps(main,indent=2));os.chmod(stage,0o600);os.replace(stage,canonical)
(P/'main.scoped-resumption.json').write_text(json.dumps(main,indent=2));os.chmod(P/'main.scoped-resumption.json',0o600)
(R/'scoped-resumption-decision.json').write_text(json.dumps({'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'accounts':['of-176774528','of-99421145'],'existingAutomatedChats':len(rows),'excludedAccount':'of-323358988','scopeRestricted':True,'paymentHoldsCleared':False,'financialCoverageCertified':False,'publicLaunchApproved':False},indent=2))

# Restore command controls and resume only the existing eligible scoped chats.
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
 if name=='twin-onlyfans-fastt-supervisor-1':expected['ONLYFANS_FASTT_SCOPE']=restricted_scope
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
report={'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'backend':'293b23dbdf8dd5e97f27d6c0bef8ef4710415c06','dashboard':'4599999761dbf2ff2b517359642d10262324867a','schema':'0097_release_controls','roles':roles,'ready':ready,'restoredExistingAutomation':True,'releaseMode':'restricted-existing-chats','accountsResumed':['of-176774528','of-99421145'],'excludedAccount':'of-323358988','publicLaunchApproved':False,'financialCoverageCertified':False,'manualFanTestsSent':False,'accountControlsChanged':False,'dailyLimit':2000}
(R/'activation-proof.json').write_text(json.dumps(report,indent=2));print(json.dumps(report))
