"""Compact model-facing catalogue without dropping products or asset evidence.

The full durable snapshot remains the validation and delivery authority.
Only repeated catalogue/audit fields are removed from the model projection.
"""
from copy import deepcopy

SHORT={'already_owned_use_existing_access':'owned','media_previously_sent_requires_explicit_resend':'sent',
    'refused_in_this_turn':'refused','price_exceeds_budget':'over_budget','product_previously_rejected':'he_declined',
    'offer_already_pending':'pending_card'}


def project(snapshot):
    result=deepcopy({k:v for k,v in snapshot.items() if k not in {'instructions','products','inventory','offer_eligibility'}})
    eligibility=snapshot.get('offer_eligibility') or {}
    # A reason that blocks every product (e.g. a fan-wide sales hold) is stated once, not per product.
    reasons=set(eligibility.values())
    shared=reasons if len(reasons)==1 and 'ok' not in reasons else set()
    if shared:result['no_new_offers']=SHORT.get(next(iter(shared)),'blocked')
    inventory=snapshot.get('inventory',{})
    products=snapshot.get('products',{})
    captions={p.get('caption_instructions','') for p in products.values()}
    common_caption=next(iter(captions)) if len(captions)==1 else None
    if common_caption:
        result['catalogue_caption_instructions']=common_caption
    compact={}
    for key,p in products.items():
        terms=inventory[key]
        files={f['media_id']:f for f in terms.get('files',[])}
        entry={name:deepcopy(p[name]) for name in
               ('category','price_cents','currency','available','requires','source')}
        if not entry['requires']: del entry['requires']  # absent means no extra condition; keeps 100 products in budget
        cents=p['price_cents']
        entry['price_display']=f"${cents//100}.{cents%100:02d} {p['currency']}"
        entry['media_ids']=deepcopy(terms['media_ids'])
        entry['preview_ids']=deepcopy(terms['preview_ids'])
        entry['catalogue_item_id']=terms['item']['id']
        reason=eligibility.get(key,'ok')
        if reason!='ok' and reason not in shared:
            # Absent = may be offered now. Otherwise a short code: discuss only, never a new card.
            entry['no_offer']=SHORT.get(reason,'blocked')
        entry['label']=terms['item']['label']
        entry['assets']=[]
        for asset in p['assets']:
            facts=files.get(asset['id'],{})
            entry['assets'].append({'id':asset['id'],'facts':deepcopy(asset['facts']),
                'kind':facts.get('kind'),
                'description_basis':deepcopy(facts.get('description_basis',{})),
                'operator_corrected':facts.get('operator_corrected',False)})
        if common_caption is None:
            entry['caption_instructions']=p.get('caption_instructions','')
        compact[key]=entry
    result['products']=compact
    # These hashes/revisions are wire-time integrity data, not writing instructions.
    for key in ('account_revision','binding_id','configuration_fingerprint','fan_revision','native_fingerprint'):
        result.pop(key,None)
    return result
