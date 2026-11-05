"""Explicit supervised subset of the existing engine, never activation authority."""
import re

from .configuration import InvalidConfiguration,ActionBlocked

EVENTS={'turn','welcome','invitation','purchase','refund','chargeback'}
ACTIONS={'reply','invite','offer','resend','alternative','continue','handover','none'}


def scope(settings):
    value=settings.get('pilot')
    if value is None:return None
    if (type(value) is not dict or set(value)!={'fan_ids','product_ids','supervisor'}
            or type(value['fan_ids']) is not list or not 1<=len(value['fan_ids'])<=10
            or any(type(f) is not int or f<=0 for f in value['fan_ids'])
            or len(set(value['fan_ids']))!=len(value['fan_ids'])
            or type(value['product_ids']) is not list or not value['product_ids']
            or any(type(p) is not str or not p for p in value['product_ids'])
            or len(set(value['product_ids']))!=len(value['product_ids'])
            or set(value['product_ids'])!={p['id'] for p in settings['products']}
            or type(value['supervisor']) is not str or not value['supervisor'].strip()
            or value['supervisor']!=settings['operational'].get('assigned_operator')):
        raise InvalidConfiguration('invalid_supervised_pilot_scope')
    # Existing sequence validation still requires complete applicable sequences.
    return value


def check(settings,fan,kind):
    value=scope(settings)
    if value is None:return
    if fan not in value['fan_ids']:raise ActionBlocked('outside_pilot_recipient_scope')
    if kind not in EVENTS:raise ActionBlocked('pilot_automation_deferred')


def decision(settings,snapshot,result):
    value=scope(settings)
    if value is None:return
    check(settings,snapshot['fan_id'],snapshot['event']['kind'])
    if (result['action'] not in ACTIONS or _specialist_permission(result)
            or (result['product_id'] is not None and result['product_id'] not in value['product_ids'])):
        raise ActionBlocked('pilot_specialist_handover_required')


def _specialist_permission(result):
    # Sexting is a conversational reply capability, not a separately fulfilled
    # service. validate_decision still resolves its current creator conditions.
    return result['permission'] is not None and not (
        result['permission']=='sexting' and result['action']=='reply')


_REMEDY_COMMITMENT=re.compile(
    r"\b(?:i(?:'ll| will| can| could| am going to)|we(?:'ll| will| can| could| are going to)|i'm gonna|we're gonna)\s+"
    r"(?:refund|reimburse|compensate|credit|replace|resend)\b"
    r"|\b(?:i(?:'ll| will| can)|we(?:'ll| will| can))\s+(?:give|issue|process|send)\s+"
    r"(?:(?:you|your|a|the)\s+){0,3}(?:refund|reimbursement|money back|replacement)\b"
    r"|\b(?:i(?:'ve| have)?|we(?:'ve| have)?)\s+(?:already\s+)?(?:approved|issued|processed|sent)\b[^.!?]{0,35}\b(?:refund|reimbursement|money back)\b"
    r"|\byou(?:'ll| will)\s+(?:get|receive)\s+(?:(?:your|a|the)\s+)?(?:refund|money back|replacement)\b"
    r"|\b(?:your|the)\s+(?:refund|reimbursement)\s+(?:is|was|has been)\s+(?:approved|issued|processed|sent|complete|on the way)\b",re.I)


def _unapproved_reply(result):
    from onlyfans_runtime.quote import boundary_hold
    from automations.pack_claim import has_delivery_promise
    texts=[p.get('text','') for p in (result.get('messages') or []) if isinstance(p,dict)] or [result['text']]
    for text in texts:
        text=text.replace('’',"'")
        if boundary_hold(text):return True
        for promise in _REMEDY_COMMITMENT.finditer(text):
            # A resend action carries its own card, subject to the ordinary
            # product/receipt checks; its caption may announce that resend.
            if result['action']=='resend' and re.search(r'\bresend\b',promise.group(),re.I):continue
            if ('complaint' in result['flags'] or not re.search(r'\breplace\b',promise.group(),re.I)
                    or re.search(r'\b(?:your|ur|the|that|this)\s+(?:photos?|pics?|videos?|cards?|content|media|items?|purchases?|orders?)\b|\bfor free\b',text,re.I)):
                return True
        if 'complaint' in result['flags'] and has_delivery_promise(text):return True
    return False


def route_specialist(settings,result):
    """No unimplemented service promises, charges or automatic fulfilment."""
    if scope(settings) is None:return result
    if (_specialist_permission(result) or result['action'] in {'correct','fulfil'}
            or ('complaint' in result['flags'] and result['action'] not in {'reply','handover','none'})
            or (result['action'] not in {'none','handover'} and _unapproved_reply(result))):
        # The ordered messages array is the normal writer output now: a handover carries none, so drop the drafted ones.
        routed={**result,'action':'handover','text':'','product_id':None,'sequence':None,
            'permission':None,'resolve_obligation':None,'reason':'pilot_specialist_handover_required'}
        if 'messages' in routed:routed['messages']=[]
        return routed
    return result
