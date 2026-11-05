"""Chronological explicit input updates; a later cancellation/budget fences older consent."""
from copy import deepcopy
def cancelled(message):
    from .request_evidence import send_cancelled
    return send_cancelled(message.get('text',''))

def explicit_updates(messages):
    result=[]
    for m in messages:
        from .request_evidence import stops_all_sends,budget_evidence
        budgets=budget_evidence(m.get('text',''))
        if budgets:
            quote,amount=budgets[-1]
            result.append({'kind':'budget','active':True,'message_id':m['id'],'quote':quote,
                'product_id':None,'amount_cents':amount})
        if stops_all_sends(m.get('text','')):result.append({'kind':'ordinary_chat_only','active':True,'message_id':m['id'],
            'quote':m['text'],'product_id':None})
    return result


def current_requests(messages):
    from .request_evidence import has_replacement,cancellation_leaves_independent_request
    result=[]
    for message in messages:
        if not cancelled(message):
            result.append(message)
            continue
        result=[prior for prior in result if cancellation_leaves_independent_request(
            prior.get('text',''),message.get('text',''))]
        if has_replacement(message.get('text','')):
            # Keep the exact source message, including its cancellation. The
            # authority check binds to the affirmative replacement clause.
            result.append(message)
    return result


def chronological(values,messages):
    positions={m['id']:i for i,m in enumerate(messages)}
    # Exact, explicit fan statements are authority even if the model omits them.
    values=list(values)+explicit_updates(messages)
    ordered=sorted(enumerate(values),key=lambda p:(positions[p[1]['message_id']],p[0]))
    by_identity={}
    from .configuration import ActionBlocked
    for _,o in ordered:
        key=(o['kind'],o['product_id']);prior=by_identity.get(key)
        if prior is not None and prior['message_id']==o['message_id'] and any(prior.get(k)!=o.get(k) for k in ('active','amount_cents')):
            raise ActionBlocked('conflicting_constraint_observations')
        by_identity[key]=deepcopy(o)
    return sorted(by_identity.values(),key=lambda o:positions[o['message_id']])
