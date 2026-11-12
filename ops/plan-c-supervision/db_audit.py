"""Called only by the stopped manual recovery owner, with no provider credentials."""
import json,os
from pathlib import Path
os.environ['DATABASE_URL']=Path('/run/pilot-database-url').read_text().strip()
from onlyfans_runtime.store import Store
from onlyfans_runtime.monitor_incident_recovery import record_resolution
proof=json.loads(Path('/proof/receipt.json').read_bytes())
# The reviewed identity the resolver was configured with. The database owner checks it against the persisted scope.
context=json.loads(os.environ.get('TWIN_SUPERVISION_CONTEXT') or '{}')
if (set(context)!={'account','binding','epoch','providerAccount','fans','publicUrl'}
        or context['account']!=os.environ['TWIN_SUPERVISION_ACCOUNT']
        or proof.get('account_id')!=context['account'] or proof.get('binding_id')!=context['binding']
        or proof.get('connection_epoch')!=context['epoch']):
    raise SystemExit('explicit account supervision context matching the receipt is required')
actor=json.loads(Path('/run/pilot-identity.json').read_bytes())['actor']
store=Store(os.environ['DATABASE_URL'])
try:
 result=record_resolution(store,os.environ['TWIN_SUPERVISION_TENANT'],os.environ['TWIN_SUPERVISION_ACCOUNT'],proof=proof,
                          actor=actor,command_key=proof['audit_command_key'],
                          expected_identity={'provider_account':context['providerAccount'],'fans':context['fans']})
 print(json.dumps({'recorded':result['recorded'],'evidenceSha256':result['evidenceSha256'],
                   'sendingEnabled':result['sendingEnabled']},sort_keys=True))
finally:store.close()
