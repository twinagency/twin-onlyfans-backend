"""Isolated final-source module sweep, retaining failures and per-process logs."""
import ast
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

ROOT=Path('/workspace/scratch/6112cde2eedb')
OLD=Path('/workspace/scratch/86d4770bae26')
REPO=OLD/'backend'
OUT=ROOT/os.environ.get('QUALIFICATION_OUTPUT','acceptance')
PYTHON='/workspace/scratch/9a82163d809e/qualification-venv/bin/python'
PG=OLD/'pg-runtime/usr/lib/postgresql/16/bin'
DATA=Path('/dev/shm/of-resumed-6112cde2eedb-'+OUT.name)
OUT.mkdir(exist_ok=True)

def manifest():
    paths=[p for group in ('service','scripts') for p in (REPO/group).rglob('*')
           if p.is_file() and '__pycache__' not in p.parts and p.suffix in ('.py','.sql','.mjs','.ts','.json')]
    return {p.relative_to(REPO).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}

def main():
    frozen=manifest()
    (OUT/'source-before.json').write_text(json.dumps(frozen,indent=2,sort_keys=True))
    pg_env={k:v for k,v in os.environ.items() if k in ('PATH','LANG','LC_ALL')}
    pg_env.update(LD_PRELOAD=str(OLD/'pg-runtime/qualification_uid.so'),
                  LD_LIBRARY_PATH=str(OLD/'pg-runtime/usr/lib/x86_64-linux-gnu'))
    assert not DATA.exists(), 'fresh owned cluster required'
    with (OUT/'initdb.log').open('w') as log:
        subprocess.run([str(PG/'initdb'),'-D',str(DATA),'-U','of_qualification','-A','trust','--no-locale','-E','UTF8'],
                       env=pg_env,stdout=log,stderr=subprocess.STDOUT,check=True)
    with (OUT/'postgres.log').open('w') as log:
        server=subprocess.Popen([str(PG/'postgres'),'-D',str(DATA),'-h','127.0.0.1','-p','58107','-k','',
            '-c','timezone=UTC','-c','max_connections=300','-c','shared_buffers=128MB'],env=pg_env,stdout=log,stderr=subprocess.STDOUT)
        try:
            for _ in range(100):
                if server.poll() is not None:raise RuntimeError('owned PostgreSQL failed')
                if 'ready to accept connections' in (OUT/'postgres.log').read_text():break
                time.sleep(.1)
            else:raise RuntimeError('owned PostgreSQL not ready')
            print('POSTGRES READY',server.pid,flush=True)
            env={k:v for k,v in os.environ.items() if k in ('PATH','LANG','LC_ALL')}
            env.update(PYTHON_DOTENV_DISABLED='1',PYTHONDONTWRITEBYTECODE='1',PYTHONUNBUFFERED='1',
                ONLYFANS_TEST_PG_URL='postgresql+psycopg2://of_qualification@127.0.0.1:58107/postgres',
                ONLYFANS_TEST_PG_EMPTY_TEMPLATE='1',ONLYFANS_TEST_PG_BIN=str(PG),
                ONLYFANS_TEST_DASHBOARD=str(OLD/'twin-dashboard'),
                ONLYFANS_RELEASE_SERVICE_DIR=str(OLD/'released/service'),
                ONLYFANS_COMPATIBLE_ROLLBACK_DIR=str(OLD/'fceb624-rollback'),
                CODEX_PRIMARY_RUNTIME_NODE_MODULES='/opt/codex/runtimes/codex-primary-runtime/dependencies/node/node_modules',
                PREVIEW_CHROME=str(OLD/'browser-tools/chromium'),
                PYTHONPATH=os.pathsep.join(str(REPO/p) for p in ('scripts/onlyfans_test_network','service','service/tests')))
            paths=sorted((REPO/'service/tests').glob('test_*.py'))
            serial=[p for p in paths if any(x in p.name for x in ('throughput','capacity','concurrent','load','pacing','r8_',
                'http_pg','release_rehearsal','native_pilot_service'))]
            ordinary=[p for p in paths if p not in serial]
            results={}
            meta={'started_utc':datetime.now(timezone.utc).isoformat(),'base_remote_head':os.environ.get('QUALIFICATION_REMOTE_HEAD','766a7992908e5706d024b90e083d6a260f93a8c5'),
                  'ordinary_parallelism':3,'timing_browser_and_native_service':'sequential after ordinary modules',
                  'transports':'synthetic; loopback Python socket guard; scrubbed environment',
                  'postgres':'16.15 tmpfs with retained test-only UID shim, not production image/durability evidence',
                  'source_hash_count':len(frozen),'status':'running'}
            def save():
                temp=OUT/'results.tmp.json'
                temp.write_text(json.dumps({'environment':meta,'results':[results[p.name] for p in paths if p.name in results]},indent=2))
                temp.replace(OUT/'results.json')
            def run(p):
                tree=ast.parse(p.read_text())
                imported=any(isinstance(n,ast.Import) and any(x.name.split('.')[0]=='pytest' for x in n.names)
                    or isinstance(n,ast.ImportFrom) and (n.module or '').split('.')[0]=='pytest' for n in ast.walk(tree))
                tests=any(isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name.startswith('test_') for n in tree.body)
                entry=any(isinstance(n,ast.If) and isinstance(n.test,ast.Compare) and isinstance(n.test.left,ast.Name)
                    and n.test.left.id=='__name__' and any(isinstance(c,ast.Constant) and c.value=='__main__' for c in n.test.comparators) for n in tree.body)
                cmd=[PYTHON]+(['-m','pytest','-q','--tb=short'] if imported or tests and not entry else [])+[str(p)]
                t=time.monotonic()
                with (OUT/(p.name+'.log')).open('w') as f:
                    try:code=subprocess.run(cmd,cwd=REPO,env=env,stdout=f,stderr=subprocess.STDOUT,timeout=900).returncode
                    except subprocess.TimeoutExpired:code=None
                return {'module':p.name,'command':cmd,'exit_code':code,'status':'pass' if code==0 else 'timeout' if code is None else 'fail',
                        'seconds':round(time.monotonic()-t,3),'source_sha256':frozen[p.relative_to(REPO).as_posix()]}
            save()
            with ThreadPoolExecutor(max_workers=3) as pool:
                for future in as_completed([pool.submit(run,p) for p in ordinary]):
                    row=future.result();results[row['module']]=row;save()
                    print(len(results),'/',len(paths),row['module'],row['status'],row['seconds'],flush=True)
            for p in serial:
                row=run(p);results[row['module']]=row;save()
                print(len(results),'/',len(paths),row['module'],row['status'],row['seconds'],flush=True)
            meta.update(finished_utc=datetime.now(timezone.utc).isoformat(),source_unchanged=manifest()==frozen,
                        status='finished',passed=sum(x['status']=='pass' for x in results.values()),
                        failed=sum(x['status']!='pass' for x in results.values()))
            save();print(json.dumps(meta),flush=True)
            assert meta['source_unchanged'],'source changed during final sweep'
        finally:
            server.send_signal(signal.SIGTERM)
            try:server.wait(timeout=30)
            except subprocess.TimeoutExpired:server.kill();server.wait()
            print('OWNED POSTGRES STOPPED',server.returncode,flush=True)

if __name__=='__main__':main()
