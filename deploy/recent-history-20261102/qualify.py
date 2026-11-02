"""Bounded synthetic tests for the recorded image, with a disposable internal DB."""
import json,pathlib,subprocess,sys,time,uuid
P=pathlib.Path(sys.argv[1]);manifest=json.loads((P/'build.json').read_text())
def run(a):return subprocess.run(a,check=True,capture_output=True,text=True).stdout
network='recent-qual-'+uuid.uuid4().hex[:8];db=network+'-db'
run(['docker','network','create','--internal',network])
try:
 run(['docker','run','-d','--name',db,'--network',network,'--memory','512m','--cpus','1','-e','POSTGRES_USER=synthetic','-e','POSTGRES_PASSWORD=synthetic','-e','POSTGRES_DB=postgres','ca0bd484cb98'])
 for _ in range(60):
  if subprocess.run(['docker','exec',db,'pg_isready','-U','synthetic','-d','postgres'],capture_output=True).returncode==0:break
  time.sleep(.25)
 else:raise RuntimeError('Disposable PG unavailable')
 url='postgresql+psycopg2://synthetic:synthetic@'+db+':5432/postgres'
 args=['docker','run','--rm','--network',network,'--memory','1g','--cpus','1','--entrypoint','python']
 env={'PYTHONPATH':'/app/service:/app/service/tests','PYTHON_DOTENV_DISABLED':'1','TWIN_FASTT_CONFIG_ISOLATED':'1','ONLYFANS_PROCESS_ROLE':'worker','ONLYFANS_TEST_PG_URL':url,'ONLYFANS_TEST_PG_EMPTY_TEMPLATE':'1','DATABASE_URL':url.replace('psycopg2','asyncpg')}
 for k,v in env.items():args+=['-e',k+'='+v]
 args+=[manifest['testImage'],'-m','pytest','-q','--tb=short','--disable-warnings',
  'tests/test_recent_history_onboarding_pg.py','tests/test_history_pilot_measurement.py',
  'tests/test_parallel_history_scheduler.py',
  'tests/test_profile_learning_cohort.py','tests/test_onlyfans_connection_states_pg.py','tests/test_onlyfans_connections_pg.py',
  'tests/test_onlyfans_launch_personality_workflow.py::test_structured_approved_onboarding_excludes_private_data_and_beats_archive_claims',
  'tests/test_onlyfans_launch_personality_workflow.py::test_structured_onboarding_conflicts_block_existing_approval_atomically']
 start=time.monotonic()
 with (P/'qualification.log').open('w') as out:result=subprocess.run(args,stdout=out,stderr=subprocess.STDOUT)
 record={'revision':manifest['revision'],'exitCode':result.returncode,'elapsedSeconds':time.monotonic()-start,'tail':(P/'qualification.log').read_text()[-1800:],'productionChanged':False}
 (P/'qualification.json').write_text(json.dumps(record,indent=2));print(json.dumps(record),flush=True)
 assert result.returncode==0,'Exact-base qualification failed'
finally:
 subprocess.run(['docker','rm','-fv',db],capture_output=True);subprocess.run(['docker','network','rm',network],capture_output=True)
