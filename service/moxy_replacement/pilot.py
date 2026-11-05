"""Explicit supervised subset of the existing engine, never activation authority."""
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


def route_specialist(settings,result):
    """No unimplemented service promises, charges or automatic fulfilment."""
    if scope(settings) is None:return result
    if _specialist_permission(result) or result['action'] in {'correct','fulfil'} or 'complaint' in result['flags']:
        # The ordered messages array is the normal writer output now: a handover carries none, so drop the drafted ones.
        routed={**result,'action':'handover','text':'','product_id':None,'sequence':None,
            'permission':None,'resolve_obligation':None,'reason':'pilot_specialist_handover_required'}
        if 'messages' in routed:routed['messages']=[]
        return routed
    return result
