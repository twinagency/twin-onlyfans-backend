import pathlib,json,subprocess,os,hashlib,time,datetime,fcntl,base64
ROOT=pathlib.Path('/var/lib/twin-release/astra-compatible-20261028');P=ROOT/'cutover-private';B=ROOT/'production-backup'
IMAGE='sha256:b6a39c70a5b7ac10d898861db608bba1cc3cfa6cfcb77ca9ff56d409c7886c9e'
def run(args,timeout=180,input=None):return subprocess.check_output(args,text=True,input=input,stderr=subprocess.PIPE,timeout=timeout).strip()
def save(p,x):p.write_text(json.dumps(x,indent=2));os.chmod(p,0o600)
lock=(ROOT/'cutover.lock').open('w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
assert not(B/'completed.json').exists()and not(B/'crm.twinof').exists()
assert json.loads((ROOT/'approved-validation.json').read_text())['aggregateAccepted'] is True
stopped=[]
for name in ['next-app','twin-onlyfans-api-1','twin-onlyfans-creator-sign-in-1','twin-onlyfans-fastt-supervisor-1','twin-onlyfans-worker-1','twin-onlyfans-managed-proxy-1']:
 old=json.loads((P/(name+'.json')).read_text());d=json.loads(run(['docker','inspect',name]))[0]
 assert d['Id']==old['Id']and d['Image']==old['Image']and sorted(d['Config']['Env'])==sorted(old['Config']['Env'])
 assert not d['State']['Running']and not d['State']['OOMKilled']
 if name!='twin-onlyfans-managed-proxy-1':assert d['State']['ExitCode']!=137,'A sender/admission role was forcibly stopped'
 stopped.append({'name':name,'exit':d['State']['ExitCode'],'finishedAt':d['State']['FinishedAt'],'forcedLegacyBrokerStop':name=='twin-onlyfans-managed-proxy-1'and d['State']['ExitCode']==137})
save(B/'stopped.json',stopped)
# The old Python PID 1 broker ignores default SIGTERM. Future compose shutdown uses Python's interrupt handler.
path=P/'broker.forward.json';config=json.loads(path.read_text());config['services']['managed-proxy']['stop_signal']='SIGINT';save(path,config)
prepared=json.loads((ROOT/'prepared-cutover.json').read_text());prepared['files']['broker.forward']=hashlib.sha256(path.read_bytes()).hexdigest();prepared['configurationChanges'].append('broker SIGINT shutdown');save(ROOT/'prepared-cutover.json',prepared)
save(ROOT/'broker-drain-proof.json',{'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'legacyBrokerForcedStop':True,'senderDrainedCleanly':True,'futureStopSignal':'SIGINT','databaseQuiescenceRequiredBeforeBackup':True})
print(json.dumps({'phase':'all-writers-stopped','roles':len(stopped),'senderDrainedCleanly':True}),flush=True)
runner=['docker','run','--rm','--network','host','--cpus','1','--memory','1g','--entrypoint','python','--workdir','/app/service','--env-file',str(P/'recovery.env'),'--mount','type=bind,source='+str(B)+',target=/backup',IMAGE]
fingerprint=r'''
import os,json,hashlib
from sqlalchemy import create_engine,text,inspect
engine=create_engine(os.environ['ONLYFANS_RECOVERY_URL'])
result={}
with engine.connect() as c:
 c.exec_driver_sql("SET timezone='UTC'; SET statement_timeout='90s'; SET lock_timeout='5s'")
 assert c.scalar(text("SELECT version_num FROM alembic_version"))=='0093_conversation_default_off'
 others=c.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND backend_type='client backend'"))
 assert others==0, 'Another database client remains after writer drain'
 for schema in ('public','onlyfans_proxy','fastt_runtime'):
  for table in inspect(c).get_table_names(schema=schema):
   columns=[x['name']for x in inspect(c).get_columns(table,schema=schema)]
   query='SELECT count(*) n,md5(string_agg(h,chr(10) ORDER BY h)) h FROM (SELECT md5(row_to_json(t)::text) h FROM "'+schema+'"."'+table+'" t) q'
   row=c.execute(text(query)).mappings().one()
   result[schema+'.'+table]={'columns':columns,'rows':row['n'],'digest':row['h']}
open('/backup/before.json','w').write(json.dumps(result,sort_keys=True))
print(json.dumps({'tables':len(result),'messages':result['public.messages']['rows'],'source':'0093_conversation_default_off'}))
'''
print(run(runner+['-c',fingerprint],timeout=240),flush=True)
print(run(runner+['-m','onlyfans_runtime.operations','backup','/backup/crm.twinof','--pg-bin','/opt/pg16','--include-proxy','--source-revision','0093_conversation_default_off'],timeout=150),flush=True)
session=r'''
import os,json,hashlib,urllib.request,datetime,base64
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
url=os.environ['SUPABASE_URL'].rstrip('/')
assert url=='https://aouaenqayfltoqrmpitg.supabase.co'
headers={'apikey':os.environ['SUPABASE_SERVICE_KEY'],'Authorization':'Bearer '+os.environ['SUPABASE_SERVICE_KEY'],'Range-Unit':'items'}
payload={'project':'aouaenqayfltoqrmpitg','at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'tables':{}}
for table,key in [('dashboard_owner_accounts','id'),('dashboard_owner_sessions','token_hash')]:
 rows=[];offset=0
 while True:
  req=urllib.request.Request(url+'/rest/v1/'+table+'?select=*&order='+key,headers={**headers,'Range':str(offset)+'-'+str(offset+999)})
  with urllib.request.urlopen(req,timeout=20) as r:chunk=json.load(r)
  assert isinstance(chunk,list)
  rows.extend(chunk);offset+=len(chunk)
  if len(chunk)<1000:break
  assert offset<100000
 payload['tables'][table]=rows
raw=json.dumps(payload,sort_keys=True,separators=(',',':')).encode()
nonce=os.urandom(12);key=base64.b64decode(os.environ['ONLYFANS_BACKUP_KEY'])
aad=b'TWIN-SESSION-BACKUP-20261028-v1'
sealed=nonce+AESGCM(key).encrypt(nonce,raw,aad)
with open('/backup/session-store.aesgcm','xb')as f:f.write(sealed)
assert AESGCM(key).decrypt(sealed[:12],sealed[12:],aad)==raw
summary={'project':payload['project'],'tables':{t:{'rows':len(v),'sha256':hashlib.sha256(json.dumps(v,sort_keys=True,separators=(',',':')).encode()).hexdigest()}for t,v in payload['tables'].items()},'archiveSha256':hashlib.sha256(sealed).hexdigest(),'authenticatedReadback':True}
open('/backup/session-proof.json','w').write(json.dumps(summary,sort_keys=True));print(json.dumps(summary))
'''
sr=runner.copy();sr[sr.index(str(P/'recovery.env'))]=str(P/'session-backup.env')
print(run(sr+['-c',session],timeout=150),flush=True)
# Encrypted copy of the precise runtime configuration needed to recover this cutover.
config=r'''
import os,pathlib,tarfile,io,base64,hashlib,json
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
buf=io.BytesIO()
with tarfile.open(fileobj=buf,mode='w:gz')as t:t.add('/private',arcname='cutover-private')
nonce=os.urandom(12);aad=b'TWIN-RUNTIME-CONFIG-20261028-v1'
raw=buf.getvalue();sealed=nonce+AESGCM(base64.b64decode(os.environ['ONLYFANS_BACKUP_KEY'])).encrypt(nonce,raw,aad)
with open('/backup/runtime-config.aesgcm','xb')as f:f.write(sealed)
assert AESGCM(base64.b64decode(os.environ['ONLYFANS_BACKUP_KEY'])).decrypt(nonce,sealed[12:],aad)==raw
print(json.dumps({'configArchiveSha256':hashlib.sha256(sealed).hexdigest(),'authenticatedReadback':True}))
'''
configargs=runner.copy();configargs[configargs.index(IMAGE):configargs.index(IMAGE)]=['--mount','type=bind,source='+str(P)+',target=/private,readonly']
print(run(configargs+['-c',config],timeout=90),flush=True)
files={p.name:{'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'bytes':p.stat().st_size}for p in B.iterdir()if p.is_file()}
save(B/'completed.json',{'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'source':'0093_conversation_default_off','files':files})
print(json.dumps({'phase':'fresh-encrypted-backups-completed','files':files}),flush=True)

