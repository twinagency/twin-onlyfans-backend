import pathlib,json,subprocess,time,os,hashlib,datetime,fcntl
R=pathlib.Path('/var/lib/twin-release/astra-compatible-20261028');P=R/'cutover-private';B=R/'production-backup'
assert(B/'completed.json').exists()
def run(a,timeout=180,input=None):return subprocess.check_output(a,text=True,input=input,stderr=subprocess.PIPE,timeout=timeout).strip()
lock=(R/'cutover.lock').open('w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
for x in json.loads((B/'stopped.json').read_text()):assert not json.loads(run(['docker','inspect',x['name']]))[0]['State']['Running']
net='astra-live-restore-20261028';pg='astra-live-restore-pg-20261028';volume='astra-live-restore-20261028'
run(['docker','network','create','--internal',net]);run(['docker','volume','create',volume])
try:
 run(['docker','run','-d','--name',pg,'--network',net,'--network-alias','restore-postgres','--memory','768m','--cpus','1','--mount','type=volume,source='+volume+',target=/var/lib/postgresql/data','-e','POSTGRES_PASSWORD=isolated-release-restore-20261028','sha256:ca0bd484cb98bf4b24eb1010e73fb3fcbd6714d240fbc1a10eea5b7dbecb641d'])
 for i in range(30):
  if subprocess.run(['docker','exec',pg,'pg_isready','-U','postgres'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0:break
  time.sleep(1)
 args=['docker','run','--rm','--network',net,'--cpus','1','--memory','1g','--env-file',str(P/'recovery.env'),'-e','ONLYFANS_RECOVERY_URL=postgresql+psycopg2://postgres:isolated-release-restore-20261028@restore-postgres/postgres','--entrypoint','python','--workdir','/app/service','--mount','type=bind,source='+str(B)+',target=/backup','sha256:b6a39c70a5b7ac10d898861db608bba1cc3cfa6cfcb77ca9ff56d409c7886c9e']
 print(run(args+['-m','onlyfans_runtime.operations','restore','/backup/crm.twinof','--pg-bin','/opt/pg16'],timeout=150),flush=True)
 verify=r'''
import os,json
from sqlalchemy import create_engine,text,inspect
from db.schema import check_schema,SchemaMismatch
from native_pilot.ledger import Ledger,Refused
e=create_engine(os.environ['ONLYFANS_RECOVERY_URL']);before=json.load(open('/backup/before.json'));after={}
with e.connect()as c:
 c.exec_driver_sql("SET timezone='UTC'; SET statement_timeout='90s'; SET lock_timeout='5s'")
 for name,item in before.items():
  if name=='public.alembic_version':continue
  schema,table=name.split('.')
  row=c.execute(text('SELECT count(*) n,md5(string_agg(h,chr(10) ORDER BY h)) h FROM (SELECT md5(row_to_json(t)::text) h FROM "'+schema+'"."'+table+'" t) q')).mappings().one()
  assert row['n']==item['rows']and row['h']==item['digest'],name+' restore mismatch'
  after[name]={'rows':row['n'],'digest':row['h']}
 assert c.scalar(text("SELECT status FROM onlyfans_restore.fence"))=='quarantined'
 assert c.scalar(text("SELECT version_num FROM alembic_version"))=='restore_quarantine_0097'
 try:check_schema(c);raise AssertionError('Restore unexpectedly ready')
 except SchemaMismatch:pass
result={'matchedTables':len(after),'messages':after['public.messages']['rows'],'quarantined':True,'releasePermitted':False}
open('/backup/restore-proof.json','w').write(json.dumps(result));print(json.dumps(result))
'''
 print(run(args+['-c',verify],timeout=240),flush=True)
finally:
 # This is the newly-created isolated restore only, never the production DB.
 subprocess.run(['docker','rm','-f',pg],stdout=subprocess.DEVNULL)
 subprocess.run(['docker','volume','rm',volume],stdout=subprocess.DEVNULL)
 subprocess.run(['docker','network','rm',net],stdout=subprocess.DEVNULL)
print(json.dumps({'phase':'fresh-backup-restore-verified'}))
