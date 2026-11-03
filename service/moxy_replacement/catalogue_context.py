"""Compact model-facing catalogue without dropping products or asset evidence.

The full durable snapshot remains the validation and delivery authority.
Only repeated catalogue/audit fields are removed from the model projection.
"""
from copy import deepcopy

def project(snapshot):
    result=deepcopy({k:v for k,v in snapshot.items() if k not in {'instructions','products','inventory'}})
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
        entry['media_ids']=deepcopy(terms['media_ids'])
        entry['preview_ids']=deepcopy(terms['preview_ids'])
        entry['catalogue_item_id']=terms['item']['id']
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
