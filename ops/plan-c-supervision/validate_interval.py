"""Validate every supported delivery using the existing app's event classifier."""
import base64
import hashlib
import json
from pathlib import Path
from fans_api_adapter.event_scope import classify_raw, SUPPORTED, IRRELEVANT, MATERIAL
from fans_api_adapter.queue_review import reviewed_queue_delivery
from conversation_engine.conflicting_webhook import reviewed_conflicting_delivery
import re
from incident_state import (WEBHOOK_INCIDENT,REPEATED_WEBHOOK_INCIDENT,incident_spec,CLASSES,READINESS,READINESS_CAVEAT,
    journal_class,evidence_reference_ok,named_sends_ok)
from delivery_history import canon,sha,retry_index,retry_state
FANS=frozenset((318022380,601164546))
REQUIRED_PILOT_RECEIPT='evt_62d643e7c8006e2e7e5dd61e22d3ec10eb0bb0f0'
PROVIDER='acct_7bf8540de5a84f459058490fee26c47f'
URL='https://peterson-blacks-quit-ceremony.trycloudflare.com/onlyfans/webhooks/plan-c'

def _readiness(root,manifest):
    """readiness_unavailable needs more than a clean interval: the journal, the sender, the scope and every
    unresolved send are re-proved here from retained bytes, and the operator's attestation must match them."""
    try:
        journal=(root/'journal.txt').read_bytes()
        sends=json.loads((root/'unresolved-sends.json').read_bytes())
        attestation=json.loads((root/'operator-attestation.json').read_bytes())
    except (OSError,ValueError):
        raise RuntimeError('readiness evidence is missing') from None
    if journal_class(journal)!=READINESS:
        raise RuntimeError('journal does not show exactly the readiness stop')
    if manifest.get('sender_absent') is not True or manifest.get('scope_status')!='stopped':
        raise RuntimeError('sender must be absent and the scope stopped')
    if (type(attestation) is not dict or set(attestation)!={'evidence_reference','named_sends','cause_caveat'}
            or not evidence_reference_ok(attestation['evidence_reference']) or not named_sends_ok(attestation['named_sends'])
            or attestation['cause_caveat']!=READINESS_CAVEAT):
        raise RuntimeError('operator attestation is missing or weakened')
    if not named_sends_ok(sends) or sends!=attestation['named_sends']:
        raise RuntimeError('unresolved sends are not exactly the sends the operator named')
    return {'cause_caveat':READINESS_CAVEAT,'evidence_reference':attestation['evidence_reference'],'unresolved_sends':sends,
            'journal_stop':READINESS,'sender_absent':True,'scope_status':'stopped'}

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
    if len({canon(r) for r in rows})!=len(rows):
        raise RuntimeError('duplicate delivery rows in the retained interval listing')
    class_based=manifest.get('resolution_kind')=='class_based'
    if class_based:
        # Class/evidence-based incident: identity and revision are bound by the receipt and the
        # database owner; here the evidence must be internally coherent and for this URL.
        if (incident_spec(manifest.get('incident_sha256')) is not None or manifest.get('classification') not in CLASSES
                or type(manifest.get('scope_revision')) is not int or type(manifest['scope_revision']) is bool
                or not re.fullmatch('[a-f0-9]{32}',str(manifest.get('incident_id','')))):
            raise RuntimeError('class-based incident evidence is malformed')
    elif 'incident_sha256' in manifest:
        spec=incident_spec(manifest['incident_sha256'])
        if spec is None or manifest.get('classification')!=spec['classification'] or manifest.get('scope_revision')!=spec['scope_revision']:
            raise RuntimeError('incident hash, classification or scope changed')
    if class_based or 'delivery_url' in manifest:
        if (manifest.get('delivery_url')!=URL or manifest.get('delivery_url_sha256')!=sha(URL.encode())
                or re.fullmatch('[a-f0-9]{64}',str(manifest.get('config_version',''))) is None):
            raise RuntimeError('delivery URL or subscription version changed')
    if 'delivery_page_count' in manifest:
        page=deliveries['_pagination']
        if page.get('complete') is not True or page.get('pages')!=manifest['delivery_page_count']:
            raise RuntimeError('delivery page evidence is incomplete or changed')
        if len(list(root.glob('deliveries-page-*.json')))<manifest['delivery_page_count']:
            raise RuntimeError('retained delivery pages are missing')
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
        full_rows={canon(r) for r in rows}
        if any(canon(r) not in full_rows for r in recent['data']):
            raise RuntimeError('incident window has unmatched reported delivery')
    readiness=_readiness(root,manifest) if class_based and manifest.get('classification')==READINESS else None
    receipts={r['key']:r for r in local['receipts']}
    findings=[];matched=[];superseded=[]
    ok_index=retry_index(rows,lambda r:r.get('succeeded') is True and type(r.get('status_code')) is int
                         and 200<=r['status_code']<300 and r.get('url')==URL and type(r.get('payload')) is dict
                         and r['payload'].get('account_id')==PROVIDER)
    for row in rows:
        key=row.get('idempotency_key');payload=row.get('payload')
        if (row.get('succeeded') is not True or type(row.get('status_code')) is not int
                or not 200<=row['status_code']<300 or row.get('url')!=URL
                or type(payload) is not dict or payload.get('account_id')!=PROVIDER):
            # A failed attempt is reconciled only by a verified successful retry of the same event, destination and payload.
            if retry_state(row,ok_index)=='resolved':
                superseded.append(key);continue
            raise RuntimeError('failed or ambiguous provider delivery')
        raw=json.dumps(payload,separators=(',',':')).encode()
        verdict=classify_raw(raw)
        if row.get('event')!=payload.get('event'):raise RuntimeError('provider event changed')
        if reviewed_queue_delivery(local,key,payload,PROVIDER):
            findings.append({'event_key':key,'event':payload['event'],'disposition':'audited_queue_group_review',
                             'limitation':'operator reviewed provider queue and both fan histories; no send authority'})
            continue
        if reviewed_conflicting_delivery(local,key,payload,PROVIDER,FANS):
            findings.append({'event_key':key,'event':payload['event'],'disposition':'audited_conflicting_event_review',
                             'limitation':'operator reviewed every retained variant as an outside-fan message; no send authority'})
            continue
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
    if class_based:
        if not rows:raise RuntimeError('class-based interval has no provider delivery evidence')
    elif REQUIRED_PILOT_RECEIPT not in {r['event_key'] for r in matched}:
        raise RuntimeError('known prior pilot receipt missing from full monitor window' if current_case else 'known post-incident pilot receipt missing from provider interval')
    return {**({'readiness':readiness} if readiness is not None else {}),
            'interval_valid_for_bounded_scope':True,'matched_receipts':matched,'superseded_failed_attempts':superseded,
            'outside_scope_limitations':findings,'full_event_continuity_claimed':False,
            **({'reported_incident_window_count':manifest['incident_delivery_count'],
                'anchor_context':'known prior receipt predates this incident','original_failure_subcause':'not retained'} if current_case else {})}

if __name__=='__main__':
    print(json.dumps(validate('/proof'),sort_keys=True))
