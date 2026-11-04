"""Chronological explicit input updates; a later cancellation/budget fences older consent."""
import re
from copy import deepcopy
_BUDGET=re.compile(r"\b(?:my\s+)?budget\s+(?:is\s+|of\s+|[:=]\s*)?(?:(?:only|just)\s+)?\$([0-9]{1,5}(?:\.[0-9]{1,2})?)\b",re.I)
_CANCEL=re.compile(r"\b(?:cancel(?:\s+(?:that|it|my request|the request))?|don'?t send|do not send|never ?mind|hold off(?: on sending)?)\b",re.I)

def cancelled(message):return bool(_CANCEL.search(message.get('text','')))

def explicit_updates(messages):
    result=[]
    for m in messages:
        for match in _BUDGET.finditer(m.get('text','')):
            result.append({'kind':'budget','active':True,'message_id':m['id'],'quote':match.group(0),
                'product_id':None,'amount_cents':round(float(match.group(1))*100)})
        if cancelled(m):result.append({'kind':'ordinary_chat_only','active':True,'message_id':m['id'],
            'quote':m['text'],'product_id':None})
    return result


def current_requests(messages):
    last=max((i for i,m in enumerate(messages) if cancelled(m)),default=-1)
    return messages[last+1:]


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
