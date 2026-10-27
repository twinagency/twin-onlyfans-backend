import json,subprocess,pathlib
R=pathlib.Path('/var/lib/twin-release/astra-compatible-20261028')
code="import os,json;from pathlib import Path;from native_pilot.supervisor import DEFAULT_STATUS;p=Path(os.environ.get('FASTT_SUPERVISOR_STATUS') or DEFAULT_STATUS);print(p.read_text() if p.exists() else '{}')"
p=subprocess.run(['docker','exec','twin-onlyfans-fastt-supervisor-1','python','-c',code],text=True,capture_output=True,timeout=20)
assert p.returncode==0,p.stderr
v=json.loads(p.stdout);(R/'supervisor-live-proof.json').write_text(json.dumps(v,indent=2));print(json.dumps(v))
