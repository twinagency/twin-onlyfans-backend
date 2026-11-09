"""Validate every supported delivery using the existing app's event classifier."""
import base64
import hashlib
import json
from pathlib import Path
from model_transport.pilot_supervised import _event_fan
FANS=frozenset((318022380,601164546))
PROVIDER='acct_7bf8540de5a84f459058490fee26c47f'
URL='https://peterson-blacks-quit-ceremony.trycloudflare.com/onlyfans/webhooks/plan-c'

def validate(root):
    root=Path(root)
    manifest=json.loads((root/'manifest.json').read_bytes())
    deliveries=json.loads((root/'deliveries.json').read_bytes())
    local=json.loads((root/'local-receipts.json').read_bytes())
    if manifest.get('reason') or manifest.get('error_type'):
        raise RuntimeError('collection did not establish provider evidence')
    if deliveries['_pagination'].get('next_page') is not None:
        raise RuntimeError('provider interval listing incomplete')
    rows=deliveries['data']
    if type(rows) is not list or len(rows)!=manifest['provider_delivery_count']:
        raise RuntimeError('interval evidence count changed')
    receipts={r['key']:r for r in local['receipts']}
    findings=[];matched=[]
    for row in rows:
        key=row.get('idempotency_key');payload=row.get('payload')
        if (row.get('succeeded') is not True or type(row.get('status_code')) is not int
                or not 200<=row['status_code']<300 or row.get('url')!=URL
                or type(payload) is not dict or payload.get('account_id')!=PROVIDER):
            raise RuntimeError('failed or ambiguous provider delivery')
        raw=json.dumps(payload,separators=(',',':')).encode()
        event,fan=_event_fan(raw)
        if row.get('event')!=event:raise RuntimeError('provider event changed')
        receipt=receipts.get(key)
        if receipt is not None:
            stored=base64.b64decode(receipt['raw'],validate=False)
            if (hashlib.sha256(stored).hexdigest()!=receipt['hash'] or json.loads(stored)!=payload
                    or receipt['status']!='accepted'):
                raise RuntimeError('conflicting retained receipt')
            matched.append({'event_key':key,'raw_sha256':receipt['hash'],'projection':receipt['projection']})
            continue
        digests=[d for d in local['digests'] if d['key']==key]
        if len(digests)!=1 or digests[0]['hold'] is not False:
            raise RuntimeError('digest evidence is missing or conflicting')
        if fan is None or fan in FANS:
            raise RuntimeError('digest-only evidence may affect pilot')
        findings.append({'event_key':key,'event':event,'fan':fan,
            'local_digest':digests[0]['hash'],'disposition':'outside_approved_fans',
            'limitation':'provider object does not reproduce retained exact raw bytes'})
    return {'interval_valid_for_bounded_scope':True,'matched_receipts':matched,
            'outside_scope_limitations':findings,'full_event_continuity_claimed':False}

if __name__=='__main__':
    print(json.dumps(validate('/proof'),sort_keys=True))
