"""Called only by the stopped manual recovery owner, with no provider credentials."""
import json,os
from pathlib import Path
os.environ['DATABASE_URL']=Path('/run/pilot-database-url').read_text().strip()
from onlyfans_runtime.store import Store
from onlyfans_runtime.monitor_incident_recovery import record_resolution
proof=json.loads(Path('/proof/receipt.json').read_bytes())
actor=json.loads(Path('/run/pilot-identity.json').read_bytes())['actor']
store=Store(os.environ['DATABASE_URL'])
try:
 result=record_resolution(store,'plan-c-pilot','of-323358988',proof=proof,
                          actor=actor,command_key=proof['audit_command_key'])
 print(json.dumps({'recorded':result['recorded'],'evidenceSha256':result['evidenceSha256'],
                   'sendingEnabled':result['sendingEnabled']},sort_keys=True))
finally:store.close()
