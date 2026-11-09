import json,subprocess,time
from pathlib import Path
from incident_state import unresolved
p=Path('/srv/projects/twin-plan-c-pilot-20261109/supervision-v11')
try:
 if unresolved(p/'outage-open.json'):raise ValueError()
 s=json.loads((p/'budget-state.json').read_text());stamp=s['last_ok_monotonic'];now=time.monotonic()
 if s['boot_id']!=Path('/proc/sys/kernel/random/boot_id').read_text().strip() or type(stamp) not in (int,float) or not 0<=now-stamp<=30:raise ValueError()
 if s.get('scope_status') not in ('stopped','configured','transition','active'):raise ValueError()
 if subprocess.run(['systemctl','is-active','--quiet','plan-c-pilot-supervisor.service'],timeout=3).returncode!=0:raise ValueError()
except Exception:raise SystemExit('pilot supervisor preflight refused')
