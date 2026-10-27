import pathlib,json,subprocess,datetime,hashlib
R=pathlib.Path('/var/lib/twin-release/astra-compatible-20261028')
activation=json.loads((R/'activation-proof.json').read_text());since=activation['at']
def run(a,timeout=60):return subprocess.check_output(a,text=True,stderr=subprocess.PIPE,timeout=timeout).strip()
expected={'twin-onlyfans-api-1':'sha256:b4c3ec13342111c1eadc75ad224ab314b659ff60494d78513b37dbdeb67e33ec','twin-onlyfans-worker-1':'sha256:b4c3ec13342111c1eadc75ad224ab314b659ff60494d78513b37dbdeb67e33ec','twin-onlyfans-fastt-supervisor-1':'sha256:b4c3ec13342111c1eadc75ad224ab314b659ff60494d78513b37dbdeb67e33ec','twin-onlyfans-managed-proxy-1':'sha256:b4c3ec13342111c1eadc75ad224ab314b659ff60494d78513b37dbdeb67e33ec','twin-onlyfans-creator-sign-in-1':'sha256:b63d926bf03c7b6abe5a52c9bfa77b4d6821d7543d16ec1ab64155c1dc925b6e','next-app':'sha256:fccf205a34fe002c6344062305128f6b302a2e1e0c5d92caf7beac30d2a79c95'}
roles=[]
for name,img in expected.items():
 d=json.loads(run(['docker','inspect',name]))[0]
 assert d['Image']==img and d['State']['Running']and not d['State']['Restarting']and not d['State']['OOMKilled'],name+' is not stable'
 # Keep complete stdout/stderr private; publish counts of structural errors only.
 logs=subprocess.run(['docker','logs','--since',since,'--tail','500',name],text=True,capture_output=True,timeout=15)
 text=logs.stdout+logs.stderr;(R/(name+'.post-deploy.private.log')).write_text(text)
 roles.append({'name':name,'image':img,'restartCount':d['RestartCount'],'tracebacks':text.count('Traceback (most recent call last):'),'schemaMismatch':text.count('SchemaMismatch'),'oom':False})
sqlcode=r'''
import json,os,datetime
from sqlalchemy import create_engine,text
e=create_engine(os.environ['DATABASE_URL'],connect_args={'options':'-c default_transaction_read_only=on -c statement_timeout=10000'})
since=START
with e.connect()as c:
 schema=c.scalar(text('SELECT version_num FROM alembic_version'))
 accounts=[dict(r)for r in c.execute(text('SELECT account_id,enabled FROM of_runtime ORDER BY account_id')).mappings()]
 states=[dict(r)for r in c.execute(text('SELECT state,count(*) AS count FROM fastt_runtime.twin_bridge_send_attempts GROUP BY state ORDER BY state')).mappings()]
 receipts=[dict(r)for r in c.execute(text("SELECT p.account_id,r.state,count(*) AS count,count(r.provider_message_id) AS provider_ids,max(r.updated_at) AS latest FROM fastt_runtime.native_pilot_receipts r JOIN fastt_runtime.native_pilots p ON p.pilot_id=r.pilot_id WHERE r.updated_at::timestamp >= CAST(:since AS timestamptz) AT TIME ZONE 'UTC' GROUP BY p.account_id,r.state ORDER BY p.account_id,r.state"),{'since':since}).mappings()]
 inputs=[dict(r)for r in c.execute(text("SELECT state,count(*) AS count FROM fastt_runtime.native_pilot_inputs WHERE updated_at::timestamp >= CAST(:since AS timestamptz) AT TIME ZONE 'UTC' GROUP BY state ORDER BY state"),{'since':since}).mappings()]
 print(json.dumps({'schema':schema,'accounts':accounts,'sendAttemptStates':states,'recentReceipts':receipts,'recentInputStates':inputs},default=str))
'''.replace('START',repr(since))
data=json.loads(run(['docker','exec','twin-onlyfans-worker-1','python','-c',sqlcode]))
assert data['schema']=='0097_release_controls'
proof={'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'since':since,'roles':roles,'runtime':data,'manualFanTestsSent':False,'controlledPaidJourneyVerified':False}
(R/'post-deploy-proof.json').write_text(json.dumps(proof,indent=2));print(json.dumps(proof))
