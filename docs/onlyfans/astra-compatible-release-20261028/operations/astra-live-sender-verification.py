import subprocess,json,pathlib,datetime
R=pathlib.Path('/var/lib/twin-release/astra-compatible-20261028')
code="import json;from native_pilot.supervisor import read_account;from twin_bridge.coordination import store_target;print(json.dumps({a:read_account(store_target(),a).get('handle')for a in ['of-176774528','of-99421145','of-323358988']}))"
p=subprocess.run(['docker','exec','twin-onlyfans-fastt-supervisor-1','python','-c',code],capture_output=True,text=True,timeout=20);assert p.returncode==0,p.stderr
handles=json.loads(p.stdout)
p=subprocess.run(['docker','logs','--since','2026-10-27T19:44:00Z','--tail','300','twin-onlyfans-fastt-supervisor-1'],capture_output=True,text=True,timeout=20)
events=[]
for line in (p.stdout+p.stderr).splitlines():
 try:v=json.loads(line)
 except Exception:continue
 if isinstance(v,dict):events.append({k:v[k]for k in ('event','account','reason','state','scope','error')if k in v})
proof={'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'handles':handles,'events':events}
(R/'sender-startup-proof.json').write_text(json.dumps(proof,indent=2));print(json.dumps(proof))
