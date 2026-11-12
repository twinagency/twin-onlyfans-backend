"""Retain an unresolved outage, including the last confirmed healthy instant."""
import datetime,json,os
from pathlib import Path
from operation_config import load
config=load()
p=config['root']/'supervision-v11'
try:state=json.loads((p/'budget-state.json').read_text())
except Exception:state={}
record={'last_confirmed_healthy_utc':state.get('last_ok_utc'),'detected_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'systemd_result':os.environ.get('SERVICE_RESULT','unknown'),'systemd_exit_code':os.environ.get('EXIT_CODE','unknown'),'systemd_exit_status':os.environ.get('EXIT_STATUS','unknown'),'scope_revision_at_last_ok':state.get('scope_revision') if type(state.get('scope_revision')) is int else None,'resolved':False}
from incident_state import record_failure
record_failure(p,record)
