import pathlib,json,subprocess,hashlib,datetime,urllib.request,time
R=pathlib.Path('/var/lib/twin-release/astra-compatible-20261028');P=R/'cutover-private';S=R/'293b23dbdf8dd5e97f27d6c0bef8ef4710415c06'/'source'
def run(a,timeout=60):return subprocess.check_output(a,text=True,stderr=subprocess.PIPE,timeout=timeout).strip()
expected={str(p.relative_to(S/'service')):hashlib.sha256(p.read_bytes()).hexdigest()for p in (S/'service').rglob('*.py')}
hashcode="import pathlib,hashlib,json;p=pathlib.Path('/app/service');print(json.dumps({str(f.relative_to(p)):hashlib.sha256(f.read_bytes()).hexdigest() for f in p.rglob('*.py')}))"
roles=[]
for name in ['twin-onlyfans-api-1','twin-onlyfans-worker-1','twin-onlyfans-managed-proxy-1']:
 d=json.loads(run(['docker','inspect',name]))[0]
 assert d['State']['Running']and not d['State']['Restarting']and not d['State']['OOMKilled'],name+' unavailable'
 assert d['Image']=='sha256:b4c3ec13342111c1eadc75ad224ab314b659ff60494d78513b37dbdeb67e33ec'
 actual=json.loads(run(['docker','exec',name,'python','-c',hashcode]));assert actual==expected,name+' source differs'
 roles.append({'name':name,'image':d['Image'],'pythonFilesVerified':len(actual),'restartCount':d['RestartCount']})
token=dict(x.split('=',1)for x in json.loads((P/'twin-onlyfans-api-1.json').read_text())['Config']['Env']if'='in x)['ONLYFANS_API_TOKEN']
checks=[]
for url,headers in [('http://127.0.0.1:8788/healthz',{}),('http://127.0.0.1:8788/onlyfans/api/v1/ready',{'Authorization':'Bearer '+token,'X-OnlyFans-Schema-Version':'of-preview.v2'}),('http://127.0.0.1:8791/health',{})]:
 with urllib.request.urlopen(urllib.request.Request(url,headers=headers),timeout=20)as r:
  assert r.status==200;payload=json.load(r);checks.append({'path':url.split(':',2)[-1],'status':r.status,'body':payload})
signin=json.loads(run(['docker','inspect','twin-onlyfans-creator-sign-in-1']))[0]
assert signin['Image']=='sha256:b63d926bf03c7b6abe5a52c9bfa77b4d6821d7543d16ec1ab64155c1dc925b6e'and signin['State']['Running']
js="const fs=require('fs'),c=require('crypto');console.log(c.createHash('sha256').update(fs.readFileSync('/app/creator-sign-in/server.mjs')).digest('hex'))"
sig=run(['docker','exec','twin-onlyfans-creator-sign-in-1','node','-e',js]);assert sig==hashlib.sha256((S/'creator-sign-in/server.mjs').read_bytes()).hexdigest()
js="Promise.all([fetch('http://127.0.0.1:8479/health'),fetch(process.env.ONLYFANS_MANAGED_PROXY_BASE_URL+'/health')]).then(async rs=>{if(rs.some(r=>!r.ok))throw Error('health');console.log(JSON.stringify(await Promise.all(rs.map(r=>r.json()))));}).catch(()=>process.exit(1));"
signhealth=json.loads(run(['docker','exec','twin-onlyfans-creator-sign-in-1','node','-e',js]))
ledger=r'''
import os,json
from sqlalchemy import create_engine,select,text
from sqlalchemy.orm import Session
from db.schema import check_schema
from db.onlyfans_models import OfRuntime
from onlyfans_runtime.launch_operations import _ledger_core
e=create_engine(os.environ['DATABASE_URL'],connect_args={'options':'-c default_transaction_read_only=on -c statement_timeout=10000'})
with e.connect()as c:check_schema(c)
result=[]
with Session(e)as s:
 for runtime in s.scalars(select(OfRuntime).order_by(OfRuntime.account_id)):
  v=_ledger_core(s,runtime);v.pop('fingerprint',None);result.append({'account':runtime.account_id,'enabled':runtime.enabled,**v})
print(json.dumps(result))
'''
financial=json.loads(run(['docker','exec','twin-onlyfans-worker-1','python','-c',ledger]))
proof={'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'roles':roles,'sourceFiles':expected,'checks':checks,'signin':{'sourceSha256':sig,'health':signhealth},'ledger':financial,'paidReady':all(v['fresh']and not v['commerceHold']and not v['conflictingEvidence']and not v['unresolvedTransactions']for v in financial if v['enabled'])}
(R/'read-roles-proof.json').write_text(json.dumps(proof,indent=2))
print(json.dumps({k:v for k,v in proof.items()if k!='sourceFiles'}))
