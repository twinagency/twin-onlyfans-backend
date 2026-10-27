import pathlib,json,subprocess,os,hashlib,datetime,fcntl
R=pathlib.Path('/var/lib/twin-release/astra-compatible-20261028');P=R/'cutover-private';B=R/'production-backup'
assert json.loads((B/'restore-proof.json').read_text())['quarantined']
def run(a,timeout=180):
 p=subprocess.run(a,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout)
 if p.returncode:
  (B/'migration-error.private.log').write_text(p.stdout+'\n'+p.stderr)
  print(json.dumps({'error':p.stderr[-2500:]}),flush=True)
  raise RuntimeError('Migration/verification subprocess failed')
 return p.stdout.strip()
lock=(R/'cutover.lock').open('w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
for x in json.loads((B/'stopped.json').read_text()):assert not json.loads(run(['docker','inspect',x['name']]))[0]['State']['Running']
code=r'''
import os,json,time,datetime
from sqlalchemy import create_engine,text
from db.schema import upgrade,check_schema,CONVERSATION_DEFAULT_OFF,HEAD
e=create_engine(os.environ['ONLYFANS_RECOVERY_URL'],pool_size=1,max_overflow=0,connect_args={'options':'-c timezone=UTC -c lock_timeout=5000 -c statement_timeout=30000'})
with e.begin() as c:
 current=c.scalar(text('SELECT version_num FROM alembic_version'));assert current in (CONVERSATION_DEFAULT_OFF,HEAD)
 check_schema(c,expected=current)
 for table in ('messages','of_content_sync_source','of_runtime','of_content_sync_account','of_offer_import_job'):c.exec_driver_sql('ANALYZE public.'+table)
start=time.monotonic()
with e.begin()as c:
 c.exec_driver_sql("SET LOCAL statement_timeout='180s'")
 if current!=HEAD:upgrade(c)
 check_schema(c)
elapsed=time.monotonic()-start
open('/backup/migration-commit.json','w').write(json.dumps({'schema':HEAD,'seconds':elapsed,'alreadyApplied':current==HEAD}))
before=json.load(open('/backup/before.json'));matched=[];expectedChanges={}
with e.connect()as c:
 c.exec_driver_sql("SET statement_timeout='90s'")
 for name,item in before.items():
  if name in ('public.alembic_version','public.of_ledger_scan','public.of_ledger_history','public.of_content_sync_source'):
   expectedChanges[name]={'before':item['rows'],'after':c.scalar(text('SELECT count(*) FROM '+name))}
   if name!='public.of_content_sync_source':assert expectedChanges[name]['before']==expectedChanges[name]['after']
   else:assert expectedChanges[name]['after']>=item['rows']
   continue
  schema,table=name.split('.');columns=','.join('"'+v+'"'for v in item['columns'])
  row=c.execute(text('SELECT count(*) n,md5(string_agg(h,chr(10) ORDER BY h)) h FROM (SELECT md5(row_to_json(t)::text) h FROM (SELECT '+columns+' FROM "'+schema+'"."'+table+'") t) q')).mappings().one()
  assert row['n']==item['rows']and row['h']==item['digest'],name+' changed during migration'
  matched.append(name)
 assert c.scalar(text("SELECT version_num FROM alembic_version"))==HEAD
result={'schema':HEAD,'seconds':elapsed,'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'unchangedTables':matched,'expectedMigrationTables':expectedChanges}
open('/backup/migration-proof.json','w').write(json.dumps(result,indent=2));print(json.dumps(result))
'''
args=['docker','run','--rm','--network','host','--cpus','1','--memory','1g','--env-file',str(P/'recovery.env'),'--entrypoint','python','--workdir','/app/service','--mount','type=bind,source='+str(B)+',target=/backup','sha256:b6a39c70a5b7ac10d898861db608bba1cc3cfa6cfcb77ca9ff56d409c7886c9e']
print(run(args+['-c',code],timeout=600),flush=True)
# Explicit broker initialization runs against the same backed-up store, without starting inventory polling.
proxy=json.loads((P/'broker.forward.json').read_text())['services']['managed-proxy']['environment']
path=P/'broker-migration.env';path.write_text('\n'.join(k+'='+v for k,v in proxy.items())+'\n');os.chmod(path,0o600)
args=['docker','run','--rm','--network','twin-onlyfans_default','--env-file',str(path),'--entrypoint','python','--workdir','/app/service','sha256:b4c3ec13342111c1eadc75ad224ab314b659ff60494d78513b37dbdeb67e33ec','-m','managed_proxy.api','--init-schema']
print(run(args,timeout=60),flush=True)
(R/'broker-upgrade-proof.json').write_text(json.dumps({'ready':True,'at':datetime.datetime.now(datetime.timezone.utc).isoformat()}))
