from pathlib import Path
from concurrent.futures import ThreadPoolExecutor,as_completed
import ast,subprocess,os,json,time,hashlib,datetime,collections
root=Path.cwd();repo=root/'backend';out=root/'qualification-evidence/frozen-5465181-nonpg';out.mkdir(exist_ok=True)
python=str(root/'qualification-venv/bin/python')
env={k:v for k,v in os.environ.items() if k in ('PATH','LANG','LC_ALL')};env.update(PYTHONPATH=os.pathsep.join([str(repo/'scripts/onlyfans_test_network'),str(repo/'service'),str(repo/'service/tests'),str(repo)]),PYTHONDONTWRITEBYTECODE='1',PYTHON_DOTENV_DISABLED='1')
files=[p for d in ('service','scripts') for p in (repo/d).rglob('*.py')]
frozen={p.relative_to(repo).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
meta={'base_commit':'546518171972784264908344ba5f9d0c6ea49993','status':'frozen code snapshot; prerequisites separately labelled; whole release remains unqualified','source_hashes':frozen,'python':subprocess.check_output([python,'--version'],text=True).strip(),'started_utc':datetime.datetime.now(datetime.timezone.utc).isoformat()}
paths=sorted(p for p in (repo/'service/tests').glob('test_*.py') if not p.name.endswith('_pg.py'));results={}
def save():
 tmp=out/'results.tmp.json';tmp.write_text(json.dumps({'environment':meta,'results':[results[p.name] for p in paths if p.name in results]},indent=2));tmp.replace(out/'results.json')
def run(p):
 t=time.monotonic();tree=ast.parse(p.read_text());imports=any(isinstance(n,ast.Import) and any(x.name.split('.')[0]=='pytest' for x in n.names) or isinstance(n,ast.ImportFrom) and (n.module or '').split('.')[0]=='pytest' for n in ast.walk(tree));tests=any(isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name.startswith('test_') for n in tree.body);entry=any(isinstance(n,ast.If) and isinstance(n.test,ast.Compare) and isinstance(n.test.left,ast.Name) and n.test.left.id=='__name__' and any(isinstance(c,ast.Constant) and c.value=='__main__' for c in n.test.comparators) for n in tree.body)
 cmd=[python]+(['-m','pytest','-q','--tb=short'] if imports or tests and not entry else [])+[str(p)]
 with (out/(p.name+'.log')).open('w') as f:
  try:r=subprocess.run(cmd,cwd=repo,env=env,stdout=f,stderr=subprocess.STDOUT,timeout=600);code=r.returncode
  except subprocess.TimeoutExpired:code=None
 return {'module':p.name,'command':cmd,'exit_code':code,'status':'pass' if code==0 else 'fail','seconds':round(time.monotonic()-t,2),'source_sha256':frozen[p.relative_to(repo).as_posix()]}
critical=[p for p in paths if any(x in p.name for x in ('throughput','capacity','concurrent','load','pacing','r8_'))];units=[p for p in paths if p not in critical]
with ThreadPoolExecutor(max_workers=4) as pool:
 for f in as_completed([pool.submit(run,p) for p in units]):
  r=f.result();results[r['module']]=r;save()
for p in critical:r=run(p);results[r['module']]=r;save()
assert all(hashlib.sha256((repo/p).read_bytes()).hexdigest()==h for p,h in frozen.items()),'Source changed during run'
meta['finished_utc']=datetime.datetime.now(datetime.timezone.utc).isoformat();meta['counts']=dict(collections.Counter(r['status'] for r in results.values()));save();print(json.dumps({'counts':meta['counts'],'failed':[x['module'] for x in results.values() if x['status']!='pass']}))
