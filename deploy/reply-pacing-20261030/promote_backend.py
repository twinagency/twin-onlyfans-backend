"""Pinned production overlay promotion. Secrets and control evidence stay on the host."""
import fcntl,hashlib,json,os,pathlib,re,subprocess,time,urllib.request
ROOT=pathlib.Path('/var/lib/twin-release/reply-pacing-20261030-208785')
RECORD=ROOT/'promotion-2'
COMPOSE=pathlib.Path('/etc/twin-onlyfans/media-agency-release-compose.json')
SOURCE='d745017852e33df3c7a01f305f116084df2ef7bd'
NAMES={'api':'twin-onlyfans-api-1','sender':'twin-onlyfans-fastt-supervisor-1'}
SERVICES={'api':'api','sender':'fastt-supervisor'}
def run(a,**kw):return subprocess.run(a,check=True,capture_output=True,text=True,**kw)
def inspect(name):return json.loads(run(['docker','inspect',name]).stdout)[0]
def private(path,value):
 path.write_bytes(value if isinstance(value,bytes) else json.dumps(value,indent=2).encode());path.chmod(0o600)
def sql(q):return run(['docker','exec','twin-onlyfans-database-1','psql','-X','-U','twin_onlyfans','-d','twin_onlyfans','-Atqc',q]).stdout.strip()
def controls():
 queries={
 'configuration':'SELECT account_id,ai_chatter_config_json,persona,persona_facts_json,model,model_by_purpose,daily_cost_cap_cents,reasoning_effort FROM account_ai_config ORDER BY account_id',
 'runtime':'SELECT account_id,binding_id,enabled,activated_at,configuration_revision FROM of_runtime ORDER BY account_id',
 'takeover':'SELECT account_id,fan_id,mode,takeover_state,revision FROM of_conversation_state ORDER BY account_id,fan_id',
 'connections':'SELECT account_id,binding_id,owner_id,status,revision FROM of_provider_connection ORDER BY account_id',
 'limits':'SELECT account_id,enabled,limit_units,throttle_seconds,media_enabled FROM of_delivery_policy ORDER BY account_id'}
 return {k:sql("SELECT md5(coalesce(jsonb_agg(to_jsonb(x)),'[]'::jsonb)::text) FROM ("+q+") x") for k,q in queries.items()}
def flags_off():
 assert sql("SELECT count(*)=0 FROM account_ai_config WHERE coalesce((ai_chatter_config_json::jsonb)->'native_reply_pacing_enabled','false'::jsonb)<>'false'::jsonb OR coalesce((ai_chatter_config_json::jsonb)->'native_reply_format_enabled','false'::jsonb)<>'false'::jsonb")=='t','Reply switches require review'
def compose(*args):return run(['docker','compose','-p','twin-onlyfans','-f',str(COMPOSE),*args],timeout=120)
def write_config(raw):
 temp=COMPOSE.with_suffix('.reply-next');temp.write_bytes(raw);temp.chmod(0o600);os.replace(temp,COMPOSE)
def ready(role,image,before):
 for _ in range(40):
  d=inspect(NAMES[role]);state=d['State']
  if state['Running'] and state.get('Health',{}).get('Status') not in ('starting','unhealthy'):
   assert d['Image']==image,'Wrong running image'
   for key in ['Cmd','Entrypoint','User','WorkingDir']:assert d['Config'].get(key)==before['Config'].get(key),'Runtime drift: '+key
   env=lambda x:dict(v.split('=',1) for v in x['Config']['Env'])
   assert env(d)==env(before),'Runtime environment drift'
   assert sorted(d['HostConfig'].get('Binds') or [])==sorted(before['HostConfig'].get('Binds') or []),'Mount drift'
   effective=lambda x:sorted((m['Type'],m['Source'],m['Destination'],m['RW'],m.get('Propagation')) for m in x['Mounts'])
   assert effective(d)==effective(before),'Effective mount drift'
   if role=='api':
    try:
     with urllib.request.urlopen('http://127.0.0.1:8788/healthz',timeout=3) as r:
      assert r.status==200 and json.load(r)['ok'] is True
    except Exception:time.sleep(1);continue
   return
  time.sleep(1)
 raise RuntimeError('Service readiness failed: '+role)
def main():
 os.umask(0o077)
 lock=open(COMPOSE.parent/'paid-media-release.lock','a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 manifest=json.loads((ROOT/'manifest.json').read_text())
 integration=json.loads((ROOT/'integration-result.json').read_text())
 assert len(integration)==2 and all(r['exit']==0 and '114 passed' in r['result'] for r in integration),'Integration tests incomplete'
 manual=(ROOT/'api-final-pg.log').read_text();rollback=(ROOT/'sender-final-pg.log').read_text()
 assert re.search(r'\d+ passed',manual) and not any(x in manual for x in ['FAILED ','ERROR ',' failed']),'Manual PG checks incomplete'
 assert '1 passed' in rollback and 'FAILED ' not in rollback,'Rollback rehearsal incomplete'
 load=json.loads((ROOT/'load-comparison.json').read_text());assert len(load)==3 and all(r['exit']==0 for r in load),'Load checks incomplete'
 for name in ['next-app','next-app-of']:
  d=inspect(name);env=dict(v.split('=',1) for v in d['Config']['Env'])
  assert d['State']['Running'] and env.get('GIT_SHA')=='2e4dfea7acbc5fa3fe14b64dc2d3bb981b5da7db','Dashboard not compatible'
 assert sql('SELECT version_num FROM alembic_version')=='0101_contextual_offers','Schema drift'
 flags_off();original=COMPOSE.read_bytes();planned=json.loads(original)
 before={role:inspect(name) for role,name in NAMES.items()}
 for role,d in before.items():
  assert d['Image']==manifest['baseline'][role] and d['State']['Running'],'Live baseline drift'
  assert inspect(planned['services'][SERVICES[role]]['image'])['Id']==d['Image'],'Compose image drift'
  assert inspect('twin-reply-pacing-208785:'+role)['Id']==manifest['images'][role]
  planned['services'][SERVICES[role]]['image']=manifest['images'][role]
 replacement=(json.dumps(planned,indent=2)+'\n').encode()
 assert not (RECORD/'backend-compose.before.json').exists(),'Review existing deployment record'
 RECORD.mkdir(mode=0o700,exist_ok=False)
 private(RECORD/'backend-compose.before.json',original);private(RECORD/'backend-runtime.before.json',before)
 stopped=changed=False
 try:
  compose('stop','-t','60','fastt-supervisor','api');stopped=True
  assert COMPOSE.read_bytes()==original,'Concurrent compose update'
  control_before=controls();private(RECORD/'controls-before-digests.json',control_before)
  flags_off();write_config(replacement);changed=True
  migration=compose('run','--rm','--no-deps','-T','-e','PYTHONPATH=/app/service','--entrypoint','python','api','-m','native_pilot.pacing_schema','--url-env','DATABASE_URL')
  private(RECORD/'pacing-migration.log',(migration.stdout+migration.stderr).encode())
  assert sql("SELECT to_regclass('fastt_runtime.native_reply_pacing') IS NOT NULL")=='t'
  assert sql('SELECT version_num FROM alembic_version')=='0101_contextual_offers'
  assert controls()==control_before,'Migration changed controls'
  compose('up','-d','--no-deps','--no-build','--pull','never','api')
  ready('api',manifest['images']['api'],before['api'])
  compose('up','-d','--no-deps','--no-build','--pull','never','fastt-supervisor')
  ready('sender',manifest['images']['sender'],before['sender'])
  assert controls()==control_before,'Configuration/control drift during promotion'
  flags_off()
  for role,hashes in manifest['hashes'].items():
   program="import pathlib,hashlib,json; expected="+repr(hashes)+"; bad=[]\nfor name,sha in expected.items():\n raw=pathlib.Path('/app',name).read_bytes(); actual=hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\\0'+raw).hexdigest(); assert actual==sha,name\nprint('source-verified')"
   assert 'source-verified' in run(['docker','exec','-e','PYTHONDONTWRITEBYTECODE=1',NAMES[role],'python','-c',program]).stdout
  result={'deployed':True,'source':SOURCE,'images':manifest['images'],'both_switches_off':True,'schema_head_unchanged':True,'runtime_and_controls_preserved':True,'source_verified':True}
  private(ROOT/'backend-result.json',result);print(json.dumps(result))
 except Exception:
  if changed:
   assert COMPOSE.read_bytes()==replacement,'Concurrent release: refuse overwrite'
   write_config(original)
  if stopped:compose('up','-d','--no-deps','--no-build','--pull','never','api','fastt-supervisor')
  private(ROOT/'backend-result.json',{'deployed':False,'rollback_started':True,'additive_evidence_table_retained':True})
  raise
if __name__=='__main__':main()
