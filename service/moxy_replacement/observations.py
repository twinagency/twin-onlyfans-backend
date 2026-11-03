"""Evidence-backed fan constraints, separate from the writer's conversational flags."""
from copy import deepcopy
from .configuration import ActionBlocked

KINDS={'ordinary_chat_only','sales_refused','boundary_hold','financial_hardship'}


def observations(decision, snapshot):
    values=decision.get('observations',[])
    if type(values) is not list or len(values)>8:
        raise ActionBlocked('invalid_constraint_observations')
    incoming=[m for m in snapshot['history'] if m['direction']=='in']
    latest=incoming[-1] if incoming else None
    result=[];seen=set()
    for o in values:
        if (type(o) is not dict or set(o)!={'kind','active','message_id','quote','product_id'}
                or o['kind'] not in KINDS or type(o['active']) is not bool
                or type(o['message_id']) is not int or type(o['quote']) is not str
                or not o['quote'].strip() or not latest or o['message_id']!=latest['id']
                or o['quote'] not in latest['text']):
            raise ActionBlocked('constraint_requires_current_fan_evidence')
        product=o['product_id']
        if product is not None and (o['kind']!='sales_refused' or product not in snapshot['products']):
            raise ActionBlocked('invalid_constraint_scope')
        if o['kind']=='financial_hardship' and not o['active']:
            raise ActionBlocked('hardship_requires_owned_resolution')
        identity=(o['kind'],product)
        if identity in seen:raise ActionBlocked('conflicting_constraint_observations')
        seen.add(identity);result.append(deepcopy(o))
    required={'hardship':'financial_hardship','refusal':'sales_refused','boundary_reset':'boundary_hold'}
    for flag,kind in required.items():
        if flag in decision['flags'] and not any(o['kind']==kind and o['active'] for o in result):
            raise ActionBlocked('constraint_flag_requires_evidence:'+flag)
    return result


def apply(state, values, *, event_id=None):
    result=deepcopy(state)
    for o in values:
        if o['kind']=='sales_refused' and o['product_id'] is not None:
            rejected=set(result.get('rejected_products',[]))
            if o['active']:rejected.add(o['product_id'])
            else:rejected.discard(o['product_id'])
            result['rejected_products']=sorted(rejected)
        else:result[o['kind']]=o['active']
        ref=o['kind']+(':'+o['product_id'] if o['product_id'] else '')
        result.setdefault('constraint_evidence',{})[ref]={**o,'event_id':event_id}
    return result
