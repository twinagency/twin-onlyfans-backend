from pathlib import Path
import subprocess,os,time,json,hashlib,signal,sys
root=Path('/workspace/scratch/86d4770bae26');out=root/'terminal-evidence';out.mkdir(exist_ok=True)
pg=root/'pg-runtime/usr/lib/postgresql/16/bin'; pg_env=dict(os.environ,LD_PRELOAD=str(root/'pg-runtime/qualification_uid.so'),LD_LIBRARY_PATH=str(root/'pg-runtime/usr/lib/x86_64-linux-gnu'))
with (out/'postgres.log').open('w') as log:
 server=subprocess.Popen([str(pg/'postgres'),'-D','/dev/shm/of-final-sealed-86d4770bae26','-h','127.0.0.1','-p','58106','-k','','-c','timezone=UTC','-c','max_connections=300','-c','shared_buffers=128MB'],env=pg_env,stdout=log,stderr=subprocess.STDOUT)
 results=[];seen=set();python='/workspace/scratch/9a82163d809e/qualification-venv/bin/python'
 try:
  for i in range(100):
   if server.poll() is not None:raise RuntimeError('PG stopped '+str(server.returncode))
   if 'ready to accept connections' in (out/'postgres.log').read_text():break
   time.sleep(.1)
  print('POSTGRES READY',server.pid,flush=True)
  while True:
   jobs=json.loads((root/'terminal-jobs.json').read_text())
   todo=[j for j in jobs if j['name'] not in seen]
   if not todo:
    if (root/'linux-finish').exists():break
    time.sleep(1);continue
   j=todo[0];name=j['name'];repo=root/j.get('repo','backend');seen.add(name)
   env={k:v for k,v in os.environ.items() if k in ('PATH','LANG','LC_ALL')};env.update(PYTHON_DOTENV_DISABLED='1',PYTHONDONTWRITEBYTECODE='1',PYTHONUNBUFFERED='1',ONLYFANS_TEST_PG_URL='postgresql+psycopg2://of_qualification@127.0.0.1:58106/postgres',ONLYFANS_TEST_PG_EMPTY_TEMPLATE='1',ONLYFANS_TEST_PG_BIN=str(pg),QUALIFICATION_PG_PID=str(server.pid),PYTHONPATH=os.pathsep.join(str(repo/p) for p in ['scripts/onlyfans_test_network','service','service/tests']))
   paths=[p for folder in ('service','scripts') for p in (repo/folder).rglob('*.py')];hashes={p.relative_to(repo).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
   if j.get('test'):
    cmd=[python]+j['test']
   else:
    cmd=[python]+(['-m','cProfile','-o',str(out/(name+'.prof'))] if j.get('profile') else [])+['service/native_pilot/tools/concurrent_benchmark.py','--name',name,'--out',str(out/(name+'.json'))]+j['args']
   t=time.monotonic();print('START',name,flush=True)
   with (out/(name+'.log')).open('w') as f:
    try:code=subprocess.run(cmd,cwd=repo,env=env,stdout=f,stderr=subprocess.STDOUT,timeout=j.get('timeout',900)).returncode
    except subprocess.TimeoutExpired:code=None
   unchanged=all(hashlib.sha256((repo/p).read_bytes()).hexdigest()==h for p,h in hashes.items())
   results.append(dict(name=name,repo=j.get('repo','backend'),command=cmd,exit_code=code,seconds=round(time.monotonic()-t,3),source_unchanged=unchanged,source_hashes=hashes))
   (out/'jobs.json').write_text(json.dumps(results,indent=2));print('END',name,code,'unchanged',unchanged,flush=True)
   if code: print((out/(name+'.log')).read_text()[-5000:],flush=True)
 finally:
  server.send_signal(signal.SIGTERM)
  try:server.wait(timeout=30)
  except subprocess.TimeoutExpired:server.kill();server.wait()
