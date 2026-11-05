"""Evidence-backed fan constraints, separate from the writer's conversational flags."""
from copy import deepcopy
from .configuration import ActionBlocked

import re
KINDS={'ordinary_chat_only','sales_refused','boundary_hold','financial_hardship','budget'}
BASE={'kind','active','message_id','quote','product_id'}
COMMERCIAL={'invite','offer','resend','alternative','continue'}


def _amounts(quote):
    """Dollar amounts the fan actually wrote in the quote, in cents."""
    return {round(float(n)*100) for n in re.findall(r'(?<![\d.])(\d{1,5}(?:\.\d{1,2})?)(?![\d.])',quote)}


def observations(decision, snapshot):
    values=decision.get('observations',[])
    if type(values) is not list or len(values)>8:
        raise ActionBlocked('invalid_constraint_observations')
    from .conversation import pending_messages
    incoming={m['id']:m for m in pending_messages(snapshot)}
    result=[]
    for o in values:
        if (type(o) is not dict or o.get('kind') not in KINDS
                or set(o)!=(BASE|{'amount_cents'} if o.get('kind')=='budget' else BASE)
                or type(o['active']) is not bool
                or type(o['message_id']) is not int or type(o['quote']) is not str
                or not o['quote'].strip() or o['message_id'] not in incoming
                or o['quote'] not in incoming[o['message_id']]['text']):
            # An annotation the current fan message does not evidence is dropped, not a held turn.
            # Retained restrictions live in fan state and are unaffected by dropping a new claim.
            continue
        if o['kind']=='budget' and (o['product_id'] is not None or type(o.get('amount_cents')) is not int
                or not 0<o['amount_cents']<=10000000 or (o['active'] and o['amount_cents'] not in _amounts(o['quote']))):
            continue
        if o['kind']=='budget' and o['active']:
            from .request_evidence import budget_evidence
            budgets=budget_evidence(incoming[o['message_id']]['text'],allow_personal=True)
            if not budgets or budgets[-1][1]!=o['amount_cents']:
                continue
        product=o['product_id']
        if product is not None and (o['kind']!='sales_refused' or product not in snapshot['products']):
            raise ActionBlocked('invalid_constraint_scope')
        if o['kind']=='financial_hardship' and not o['active']:
            raise ActionBlocked('hardship_requires_owned_resolution')
        from .request_evidence import supports_preference
        if not supports_preference(o,incoming[o['message_id']],snapshot):
            continue
        result.append(deepcopy(o))
    from .input_batch import chronological
    result=chronological(result,list(incoming.values()))
    required={'hardship':'financial_hardship','refusal':'sales_refused','boundary_reset':'boundary_hold'}
    for flag,kind in required.items():
        if flag in decision['flags'] and not any(o['kind']==kind and o['active'] for o in result):
            if decision.get('action') in COMMERCIAL:
                raise ActionBlocked('constraint_flag_requires_evidence:'+flag)
            # A non-commercial reply loses only the unsupported pause flag; nothing is sold because of it.
            decision['flags']=[f for f in decision['flags'] if f!=flag]
    return result


def apply(state, values, *, event_id=None):
    result=deepcopy(state)
    for o in values:
        if o['kind']=='sales_refused' and o['product_id'] is not None:
            rejected=set(result.get('rejected_products',[]))
            if o['active']:rejected.add(o['product_id'])
            else:rejected.discard(o['product_id'])
            result['rejected_products']=sorted(rejected)
        elif o['kind']=='budget':
            if o['active']:result['budget_cents']=o['amount_cents']
            else:result.pop('budget_cents',None)
        else:result[o['kind']]=o['active']
        ref=o['kind']+(':'+o['product_id'] if o['product_id'] else '')
        result.setdefault('constraint_evidence',{})[ref]={**o,'event_id':event_id}
    return result
