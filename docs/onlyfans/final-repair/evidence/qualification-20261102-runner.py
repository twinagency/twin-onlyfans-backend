from pathlib import Path
from concurrent.futures import ThreadPoolExecutor,as_completed
import ast,subprocess,os,json,time,hashlib,tempfile,datetime,collections
root=Path('/workspace/scratch/9a82163d809e');repo=root/'backend';out=root/'qualification-evidence/frozen';out.mkdir(exist_ok=True)
python=str(root/'qualification-venv/bin/python')
baseenv={k:os.environ[k] for k in ('PATH','LANG','LC_ALL') if k in os.environ}
baseenv.update(PYTHONPATH=os.pathsep.join([str(root/'isolation'),str(repo/'service'),str(repo/'service/tests'),str(repo)]),PYTHONDONTWRITEBYTECODE='1',PYTHON_DOTENV_DISABLED='1',PYTHONUNBUFFERED='1')
source='bf16b6766bc70078e449c74c1fe07dbf34375f25';tree='ec055cf6d286bcfb755c1cafd6e5a8766e2cc30e'
assert subprocess.check_output(['git','write-tree','--missing-ok'],cwd=repo,text=True).strip()==tree
paths=sorted((repo/'service/tests').glob('test_*.py'));results={}
meta={'source_commit':source,'candidate_git_tree':tree,'local_git_base_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip(),'started_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'python':subprocess.check_output([python,'--version'],text=True).strip(),'dependencies':json.loads(subprocess.check_output([python,'-c','import importlib.metadata as m,json;print(json.dumps({n:m.version(n) for n in ["pytest","fastapi","sqlalchemy","alembic","asyncpg","psycopg2-binary"]}))'],text=True)), 'unit_module_parallelism':4,'timing_modules':'run sequentially after unit modules','network':'qualification-only socket guard rejects non-loopback Python connections; no production credentials supplied','postgres':'not available: all 139 PG modules remain unrun','windows':'Linux host: Windows runner remains platform-blocked; separate hosted Windows job did not start','whole_release_qualified':False}
def save():
 (out/'results.json').write_text(json.dumps({'environment':meta,'results':[results[p.name] for p in paths if p.name in results]},indent=2)+'\n')
def run(p):
 t=time.monotonic();parsed=ast.parse(p.read_text());uses_pytest=any((isinstance(n,ast.Import) and any(x.name.split('.')[0]=='pytest' for x in n.names)) or isinstance(n,ast.ImportFrom) and (n.module or '').split('.')[0]=='pytest' for n in ast.walk(parsed))
 command=[python]+(['-m','pytest','-q'] if uses_pytest else [])+[str(p)]
 record={'module':p.name,'command':command,'source_sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
 with tempfile.TemporaryDirectory(prefix='qualification-module-') as tmp:
  env={**baseenv,'TMPDIR':tmp,'TEMP':tmp,'TMP':tmp}
  with (out/(p.name+'.log')).open('w') as f:
   try:
    c=subprocess.run(command,cwd=repo,env=env,stdout=f,stderr=subprocess.STDOUT,timeout=600);record.update(status='pass' if c.returncode==0 else 'fail',exit_code=c.returncode)
   except subprocess.TimeoutExpired:record.update(status='timeout',exit_code=None)
 record['seconds']=round(time.monotonic()-t,2)
 return record
ready=[]
for p in paths:
 if p.name.endswith('_pg.py'):results[p.name]={'module':p.name,'status':'not_run','reason':'disposable_postgresql_unavailable','source_sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
 else:ready.append(p)
critical=[p for p in ready if any(n in p.name for n in ('throughput','capacity','concurrent','load','pacing'))]
units=[p for p in ready if p not in critical]
save()
with ThreadPoolExecutor(max_workers=4) as pool:
 for fut in as_completed([pool.submit(run,p) for p in units]):
  r=fut.result();results[r['module']]=r;save()
for p in critical:
 r=run(p);results[r['module']]=r;save()
meta['finished_utc']=datetime.datetime.now(datetime.timezone.utc).isoformat();meta['counts']=dict(collections.Counter(r['status'] for r in results.values()));save()
print(json.dumps({'counts':meta['counts'],'failed':[n for n,r in results.items() if r['status'] in ('fail','timeout')],'source_commit':source},indent=2))
