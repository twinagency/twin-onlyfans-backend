import hashlib,json,os,subprocess,time,signal
from pathlib import Path
repo=Path('/workspace/scratch/86d4770bae26/backend')
out=Path('/workspace/scratch/6112cde2eedb/supplemental-proxy')
out.mkdir(exist_ok=True)
old=Path('/workspace/scratch/86d4770bae26')
pg=old/'pg-runtime/usr/lib/postgresql/16/bin'
data=Path('/dev/shm/of-proxy-6112cde2eedb')
assert not data.exists()
pg_env={k:v for k,v in os.environ.items() if k in ('PATH','LANG','LC_ALL')}
pg_env.update(LD_PRELOAD=str(old/'pg-runtime/qualification_uid.so'),
              LD_LIBRARY_PATH=str(old/'pg-runtime/usr/lib/x86_64-linux-gnu'))
with (out/'initdb.log').open('w') as f:
    subprocess.run([str(pg/'initdb'),'-D',str(data),'-U','of_qualification','-A','trust','--no-locale','-E','UTF8'],
        env=pg_env,stdout=f,stderr=subprocess.STDOUT,check=True)
pglog=(out/'postgres.log').open('w')
server=subprocess.Popen([str(pg/'postgres'),'-D',str(data),'-h','127.0.0.1','-p','58108','-k','',
    '-c','timezone=UTC','-c','max_connections=40','-c','shared_buffers=32MB'],env=pg_env,stdout=pglog,stderr=subprocess.STDOUT)
for _ in range(100):
    if server.poll() is not None:raise RuntimeError('owned test PostgreSQL failed')
    if 'ready to accept connections' in (out/'postgres.log').read_text():break
    time.sleep(.1)
else:raise RuntimeError('owned test PostgreSQL not ready')
paths=sorted((repo/'managed_proxy_tests').glob('test_*.py'))
hashes={p.relative_to(repo).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
env={k:v for k,v in os.environ.items() if k in ('PATH','LANG','LC_ALL')}
env.update(PYTHON_DOTENV_DISABLED='1',PYTHONDONTWRITEBYTECODE='1',PYTHONUNBUFFERED='1',
    ONLYFANS_PROXY_TEST_PG_URL='postgresql://of_qualification@127.0.0.1:58108/postgres',
    PYTHONPATH=os.pathsep.join(str(repo/p) for p in ('scripts/onlyfans_test_network','service','service/tests')))
cmd=['/workspace/scratch/9a82163d809e/qualification-venv/bin/python','-m','pytest','-q','--tb=short',
     '--junitxml='+str(out/'managed-proxy.xml'),*[str(p) for p in paths]]
t=time.monotonic()
try:
    with (out/'managed-proxy.log').open('w') as log:
        code=subprocess.run(cmd,cwd=repo,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=600).returncode
finally:
    server.send_signal(signal.SIGTERM)
    try:server.wait(timeout=30)
    except subprocess.TimeoutExpired:server.kill();server.wait()
    pglog.close()
unchanged=all(hashlib.sha256((repo/p).read_bytes()).hexdigest()==h for p,h in hashes.items())
(out/'managed-proxy.json').write_text(json.dumps({'command':cmd,'exit_code':code,'seconds':round(time.monotonic()-t,3),
    'source_sha256':hashes,'source_unchanged':unchanged,'scope':'supplement to all-service module sweep; disposable owned PG'},indent=2))
print(code,unchanged)
raise SystemExit(code)
