from pathlib import Path
import ast,subprocess,os,json,time,hashlib
repo=Path('/workspace/scratch/86d4770bae26/backend');out=repo/'docs/onlyfans/final-repair/evidence/concurrent-20261102-local';out.mkdir(exist_ok=True)
python='/workspace/scratch/9a82163d809e/qualification-venv/bin/python'
env={k:v for k,v in os.environ.items() if k in ('PATH','LANG','LC_ALL')};env.update(PYTHONPATH=os.pathsep.join(str(repo/p) for p in ['scripts/onlyfans_test_network','service','service/tests']),PYTHON_DOTENV_DISABLED='1',PYTHONDONTWRITEBYTECODE='1')
names='commercial_progress commercial_full_path concurrent_path_regressions concurrent_delivery concurrent_admission_units native_reply_pacing fastt_engine_v2_units fastt_engine_v2 r8_gap_vs_concurrent r8_oldest_need_first r8_cancelled_waiter_not_abandoned native_pilot_service native_pilot_runner native_pilot_ledger native_pilot_content onlyfans_egress_isolation onlyfans_launch_integration native_reply_format turn_timing'.split()
results=[]
for name in names:
 p=repo/'service/tests'/('test_'+name+'.py');tree=ast.parse(p.read_text());imports=any(isinstance(n,ast.Import) and any(x.name.split('.')[0]=='pytest' for x in n.names) or isinstance(n,ast.ImportFrom) and (n.module or '').split('.')[0]=='pytest' for n in ast.walk(tree));entry=any(isinstance(n,ast.If) and isinstance(n.test,ast.Compare) and isinstance(n.test.left,ast.Name) and n.test.left.id=='__name__' for n in tree.body)
 cmd=[python]+(['-m','pytest','-q','--tb=short'] if (imports or not entry) and name!='fastt_engine_v2_units' else [])+[str(p)];t=time.monotonic()
 with (out/(name+'.log')).open('w') as f:
  try:code=subprocess.run(cmd,cwd=repo,env=env,stdout=f,stderr=subprocess.STDOUT,timeout=600).returncode
  except subprocess.TimeoutExpired:code=None
 results.append(dict(module=p.name,command=cmd,exit_code=code,seconds=round(time.monotonic()-t,3),sha256=hashlib.sha256(p.read_bytes()).hexdigest()))
 (out/'results.json').write_text(json.dumps(results,indent=2));print(name,code,flush=True)
