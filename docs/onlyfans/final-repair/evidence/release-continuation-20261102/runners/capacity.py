"""Final-source load checks after the full module sweep has stopped its PG."""
from pathlib import Path
import hashlib,json,os,signal,subprocess,time
ROOT=Path('/workspace/scratch/6112cde2eedb')
OLD=Path('/workspace/scratch/86d4770bae26')
REPO=OLD/'backend'
OUT=ROOT/'final-capacity'
PG=OLD/'pg-runtime/usr/lib/postgresql/16/bin'
DATA=Path('/dev/shm/of-final-capacity-6112cde2eedb')
PYTHON='/workspace/scratch/9a82163d809e/qualification-venv/bin/python'

def main():
    sweep=json.loads((ROOT/'final-acceptance/results.json').read_text())
    assert sweep['environment']['status']=='finished','do not compete with module sweep'
    OUT.mkdir(exist_ok=True)
    assert not DATA.exists(),'fresh disposable database required'
    frozen=json.loads((ROOT/'final-acceptance/source-before.json').read_text())
    assert all(hashlib.sha256((REPO/p).read_bytes()).hexdigest()==h for p,h in frozen.items())
    pg_env={k:v for k,v in os.environ.items() if k in ('PATH','LANG','LC_ALL')}
    pg_env.update(LD_PRELOAD=str(OLD/'pg-runtime/qualification_uid.so'),
                  LD_LIBRARY_PATH=str(OLD/'pg-runtime/usr/lib/x86_64-linux-gnu'))
    with (OUT/'initdb.log').open('w') as log:
        subprocess.run([str(PG/'initdb'),'-D',str(DATA),'-U','of_qualification','-A','trust','--no-locale','-E','UTF8'],
            env=pg_env,stdout=log,stderr=subprocess.STDOUT,check=True)
    with (OUT/'postgres.log').open('w') as log:
        server=subprocess.Popen([str(PG/'postgres'),'-D',str(DATA),'-h','127.0.0.1','-p','58109','-k','',
            '-c','timezone=UTC','-c','max_connections=300','-c','shared_buffers=128MB'],env=pg_env,stdout=log,stderr=subprocess.STDOUT)
        try:
            for _ in range(100):
                if server.poll() is not None:raise RuntimeError('owned PG failed')
                if 'ready to accept connections' in (OUT/'postgres.log').read_text():break
                time.sleep(.1)
            else:raise RuntimeError('owned PG not ready')
            env={k:v for k,v in os.environ.items() if k in ('PATH','LANG','LC_ALL')}
            env.update(PYTHON_DOTENV_DISABLED='1',PYTHONDONTWRITEBYTECODE='1',PYTHONUNBUFFERED='1',
                ONLYFANS_TEST_PG_URL='postgresql+psycopg2://of_qualification@127.0.0.1:58109/postgres',
                ONLYFANS_TEST_PG_EMPTY_TEMPLATE='1',QUALIFICATION_PG_PID=str(server.pid),
                PYTHONPATH=os.pathsep.join(str(REPO/p) for p in ('scripts/onlyfans_test_network','service','service/tests')))
            jobs=[
                ('frozen-100-rate1',['--prepare','8','--send','4','--rate','1','--duration','195']),
                ('frozen-100-rate10-diagnostic',['--prepare','8','--send','8','--rate','10','--duration','100']),
                ('frozen-3x20-concurrent',['--creators','3','--fans','20','--prepare','8','--send','4','--duration','195']),
                ('frozen-50x2-concurrent',['--creators','50','--fans','2','--prepare','8','--send','4','--fleet-db-pool','64','--duration','195']),
            ]
            results=[]
            for name,args in jobs:
                cmd=[PYTHON,'service/native_pilot/tools/concurrent_benchmark.py','--name',name,'--out',str(OUT/(name+'.json')),*args]
                start=time.monotonic();print('START',name,flush=True)
                with (OUT/(name+'.log')).open('w') as f:
                    code=subprocess.run(cmd,cwd=REPO,env=env,stdout=f,stderr=subprocess.STDOUT,timeout=500).returncode
                unchanged=all(hashlib.sha256((REPO/p).read_bytes()).hexdigest()==h for p,h in frozen.items())
                result={'name':name,'command':cmd,'exit_code':code,'seconds':round(time.monotonic()-start,3),'source_unchanged':unchanged}
                results.append(result);(OUT/'jobs.json').write_text(json.dumps(results,indent=2));print('END',name,code,unchanged,flush=True)
                assert unchanged
        finally:
            server.send_signal(signal.SIGTERM)
            try:server.wait(timeout=30)
            except subprocess.TimeoutExpired:server.kill();server.wait()
            print('OWNED POSTGRES STOPPED',server.returncode,flush=True)

if __name__=='__main__':main()
