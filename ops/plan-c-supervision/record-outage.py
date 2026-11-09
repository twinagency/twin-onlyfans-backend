"""Retain an unresolved outage, including the last confirmed healthy instant."""
import datetime,json,os
from pathlib import Path
p=Path('/srv/projects/twin-plan-c-pilot-20261109/supervision-v11')
try:state=json.loads((p/'budget-state.json').read_text())
except Exception:state={}
record={'last_confirmed_healthy_utc':state.get('last_ok_utc'),'detected_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'systemd_result':os.environ.get('SERVICE_RESULT','unknown'),'systemd_exit_code':os.environ.get('EXIT_CODE','unknown'),'systemd_exit_status':os.environ.get('EXIT_STATUS','unknown'),'resolved':False}
from incident_state import record_failure
record_failure(p,record)
