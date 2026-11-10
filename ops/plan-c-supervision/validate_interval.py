"""Validate every supported delivery using the existing app's event classifier."""
import base64
import hashlib
import json
from pathlib import Path
from fans_api_adapter.event_scope import classify_raw, SUPPORTED, IRRELEVANT, MATERIAL
from incident_state import WEBHOOK_INCIDENT,REPEATED_WEBHOOK_INCIDENT,incident_spec
FANS=frozenset((318022380,601164546))
REQUIRED_PILOT_RECEIPT='evt_62d643e7c8006e2e7e5dd61e22d3ec10eb0bb0f0'
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
    if 'incident_sha256' in manifest:
        spec=incident_spec(manifest['incident_sha256'])
        if spec is None or manifest.get('classification')!=spec['classification'] or manifest.get('scope_revision')!=spec['scope_revision']:
            raise RuntimeError('incident hash, classification or scope changed')
    current_case=manifest.get('incident_sha256') in (WEBHOOK_INCIDENT,REPEATED_WEBHOOK_INCIDENT)
    if current_case:
        spec=incident_spec(manifest['incident_sha256'])
        if manifest.get('classification')!=spec['classification'] or manifest.get('scope_revision')!=spec['scope_revision']:
            raise RuntimeError('incident classification or scope changed')
        recent=json.loads((root/'incident-deliveries.json').read_bytes())
        if (recent['_pagination'].get('next_page') is not None or type(recent['data']) is not list
                or type(recent.get('_meta',{}).get('_credits',{}).get('used')) is not int
                or recent['_meta']['_credits']['used']!=0 or len(recent['data'])!=manifest.get('incident_delivery_count')):
            raise RuntimeError('incident window evidence incomplete or changed')
        by_key={r.get('idempotency_key'):r for r in rows}
        if any(by_key.get(r.get('idempotency_key'))!=r for r in recent['data']):
            raise RuntimeError('incident window has unmatched reported delivery')
    receipts={r['key']:r for r in local['receipts']}
    findings=[];matched=[]
    for row in rows:
        key=row.get('idempotency_key');payload=row.get('payload')
        if (row.get('succeeded') is not True or type(row.get('status_code')) is not int
                or not 200<=row['status_code']<300 or row.get('url')!=URL
                or type(payload) is not dict or payload.get('account_id')!=PROVIDER):
            raise RuntimeError('failed or ambiguous provider delivery')
        raw=json.dumps(payload,separators=(',',':')).encode()
        verdict=classify_raw(raw)
        if verdict.cls not in (SUPPORTED,IRRELEVANT,MATERIAL):raise RuntimeError('provider event is malformed or not classified')
        event,fan=verdict.event,verdict.fan
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
        if verdict.cls==IRRELEVANT:
            disposition='validated_irrelevant'
        elif fan is None or fan in FANS:
            raise RuntimeError('digest-only evidence may affect pilot')
        else:
            disposition='outside_approved_fans'
        findings.append({'event_key':key,'event':event,'fan':fan,
            'local_digest':digests[0]['hash'],'disposition':disposition,
            'limitation':'provider object does not reproduce retained exact raw bytes'})
    if REQUIRED_PILOT_RECEIPT not in {r['event_key'] for r in matched}:
        raise RuntimeError('known prior pilot receipt missing from full monitor window' if current_case else 'known post-incident pilot receipt missing from provider interval')
    return {'interval_valid_for_bounded_scope':True,'matched_receipts':matched,
            'outside_scope_limitations':findings,'full_event_continuity_claimed':False,
            **({'reported_incident_window_count':manifest['incident_delivery_count'],
                'anchor_context':'known prior receipt predates this incident','original_failure_subcause':'not retained'} if current_case else {})}

if __name__=='__main__':
    print(json.dumps(validate('/proof'),sort_keys=True))
