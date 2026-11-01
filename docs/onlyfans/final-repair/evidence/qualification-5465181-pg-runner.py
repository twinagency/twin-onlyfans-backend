import ast,collections,datetime,hashlib,json,os,shutil,subprocess,sys,time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor,as_completed
r=Path(r'C:\Users\conta\codex-work\of-original-qualification-20261102');repo=r/'frozen-5465181';out=r/'frozen-5465181-pg-evidence';out.mkdir(exist_ok=False)
source='546518171972784264908344ba5f9d0c6ea49993'
gh=r'C:\Program Files\GitHub CLI\gh.exe'
gitcommit=json.loads(subprocess.check_output([gh,'api','repos/twinagency/twin-onlyfans-backend/git/commits/'+source],text=True))
assert gitcommit['tree']['sha']=='4a8326b3aa8b9f4c67609b5f2c3704128c468f28'
meta=json.loads((r/'frozen-5465181-provenance.json').read_text());meta['git_tree']=gitcommit['tree']['sha'];(r/'frozen-5465181-provenance.json').write_text(json.dumps(meta,indent=2))
env={k:v for k,v in os.environ.items() if k.upper() in ('SYSTEMROOT','WINDIR','PATH','TEMP','TMP','COMSPEC','PATHEXT','USERPROFILE','APPDATA','LOCALAPPDATA')}
env.update(PYTHON_DOTENV_DISABLED='1',PYTHONUNBUFFERED='1',PYTHONDONTWRITEBYTECODE='1',ONLYFANS_TEST_PG_URL='postgresql+psycopg2://of_qualification@127.0.0.1:58102/postgres',ONLYFANS_TEST_PG_EMPTY_TEMPLATE='1',ONLYFANS_TEST_PG_BIN=r'C:\Users\conta\codex-work\priority-local-pg-20261026\pgsql\bin',ONLYFANS_RELEASE_SERVICE_DIR=str(r/'released/service'),ONLYFANS_COMPATIBLE_ROLLBACK_DIR=str(r/'frozen-fceb624'),ONLYFANS_DASHBOARD_DIR=str(r/'dashboard'),ONLYFANS_TEST_DASHBOARD=str(r/'dashboard'),PYTHONPATH=os.pathsep.join(str(repo/p) for p in ['scripts/onlyfans_test_network','service','service/tests']))
# Use the exact sibling-variable names consumed by HTTP and backup fixtures.
for key in ('ONLYFANS_TEST_RELEASE_SERVICE_DIR','ONLYFANS_TEST_COMPATIBLE_ROLLBACK_DIR','ONLYFANS_TEST_DASHBOARD_DIR'):
 env[key]=env[key.replace('_TEST','')]
hashes={p.relative_to(repo).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for group in ('service','scripts') for p in (repo/group).rglob('*.py')}
paths=sorted((repo/'service/tests').glob('test_*_pg.py'));results={}
meta.update(started_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),python=sys.version,pg='PostgreSQL 16.15 task-owned loopback cluster',module_parallelism=3,timing_modules='sequential after ordinary modules',applicable_platform='Windows; Unix-only checks are failures with platform cause, not passes',whole_release_qualified=False)
def save():
 target=out/'results.tmp.json';target.write_text(json.dumps({'environment':meta,'source_hashes':hashes,'results':[results[p.name] for p in paths if p.name in results]},indent=2));target.replace(out/'results.json')
def run(p):
 t=time.monotonic();tree=ast.parse(p.read_text(encoding='utf8'))
 imports=any(isinstance(n,ast.Import) and any(x.name.split('.')[0]=='pytest' for x in n.names) or isinstance(n,ast.ImportFrom) and (n.module or '').split('.')[0]=='pytest' for n in ast.walk(tree))
 tests=any(isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name.startswith('test_') for n in tree.body)
 entry=any(isinstance(n,ast.If) and isinstance(n.test,ast.Compare) and isinstance(n.test.left,ast.Name) and n.test.left.id=='__name__' and any(isinstance(c,ast.Constant) and c.value=='__main__' for c in n.test.comparators) for n in tree.body)
 cmd=[sys.executable]+(['-m','pytest','-q','--tb=short'] if imports or tests and not entry else [])+[str(p)]
 if shutil.disk_usage(r).free<800_000_000:return {'module':p.name,'status':'blocked','reason':'task disk reserve below 800 MB; not executed','command':cmd}
 with (out/(p.name+'.log')).open('w',encoding='utf8') as f:
  try:code=subprocess.run(cmd,cwd=repo,env=env,stdout=f,stderr=subprocess.STDOUT,timeout=900).returncode
  except subprocess.TimeoutExpired:code=None
 return {'module':p.name,'command':cmd,'exit_code':code,'status':'pass' if code==0 else 'timeout' if code is None else 'fail','seconds':round(time.monotonic()-t,2),'source_sha256':hashes[p.relative_to(repo).as_posix()]}
critical=[p for p in paths if any(x in p.name for x in ('throughput','capacity','concurrent','load','pacing','r8_'))];ordinary=[p for p in paths if p not in critical]
save()
with ThreadPoolExecutor(max_workers=3) as pool:
 for f in as_completed([pool.submit(run,p) for p in ordinary]):
  row=f.result();results[row['module']]=row;save();print(row['module'],row['status'],row.get('seconds'),flush=True)
for p in critical:
 row=run(p);results[row['module']]=row;save();print(row['module'],row['status'],row.get('seconds'),flush=True)
meta['source_unchanged']=all(hashlib.sha256((repo/p).read_bytes()).hexdigest()==h for p,h in hashes.items())
assert meta['source_unchanged']
meta['finished_utc']=datetime.datetime.now(datetime.timezone.utc).isoformat();meta['counts']=dict(collections.Counter(x['status'] for x in results.values()));save();print(json.dumps(meta['counts']),flush=True)
