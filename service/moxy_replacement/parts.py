"""One durable Moxy decision, ordered through the existing one-use intents."""
import json
from copy import deepcopy
from .configuration import ActionBlocked

MEDIA_ACTIONS={'offer','continue','resend','alternative','correct','fulfil'}


def plan(decision):
    values=decision.get('messages')
    if values is None:
        return [] if decision['action'] in {'none','handover'} else [
            {'text':decision['text'],'media':decision['action'] in MEDIA_ACTIONS}]
    if decision['action'] in {'none','handover'}:
        if values==[]:return []
        raise ActionBlocked('handover_must_not_have_messages')
    if (type(values) is not list or not 1<=len(values)<=8):
        raise ActionBlocked('invalid_message_plan')
    for part in values:
        if (type(part) is not dict or set(part)!={'text','media'} or type(part['text']) is not str
                or not part['text'].strip() or type(part['media']) is not bool):
            raise ActionBlocked('invalid_message_part')
    if sum(p['media'] for p in values)!=(1 if decision['action'] in MEDIA_ACTIONS else 0):
        raise ActionBlocked('message_plan_media_mismatch')
    if values[0]['text']!=decision['text'] or sum(len(p['text'].encode()) for p in values)>12000:
        raise ActionBlocked('message_plan_text_mismatch')
    return values


def index(event):
    progress=json.loads(event.payload_json).get('_moxy_delivery',{})
    return len(progress.get('receipts',[]))


def active(event):
    original=json.loads(event.decision_json)
    values=plan(original);position=index(event)
    if position>=len(values):raise ActionBlocked('message_plan_complete')
    part=values[position];result=deepcopy(original)
    result.pop('messages',None)
    result['text']=part['text']
    if not part['media'] and not (original['action']=='invite' and position==len(values)-1):
        result.update(action='reply',product_id=None,sequence=None,request_kind='none',request_quote=None)
    # Evidence was already retained once at planning. Subsequent receipt projection
    # must never reapply an obsolete fan restriction.
    result['flags']=[];result['observations']=[]
    if ((original['action'] in MEDIA_ACTIONS and not part['media'])
            or (original['action'] not in MEDIA_ACTIONS and position!=len(values)-1)):
        result['question']=result['resolve_obligation']=None
    return result


def has_remaining(event):
    return index(event)<len(plan(json.loads(event.decision_json)))


def continuation_unchanged(before, after, own_message_id, decision):
    """Permit only our just-reconciled message; external changes cancel the tail."""
    a=deepcopy(before);b=deepcopy(after)
    a.pop('native_fingerprint',None);b.pop('native_fingerprint',None)
    # Derived from history/fan_state, which are compared below; own append changes it.
    a.pop('commercial_state',None);b.pop('commercial_state',None)
    old=a.pop('history');new=b.pop('history')
    filtered=[m for m in new if m['id']!=own_message_id]
    # Snapshot history is bounded at 100. One own append may evict one oldest row.
    if filtered!=old[-len(filtered):] or not any(m['id']==own_message_id for m in new):return False
    if decision['action'] in {'offer','continue','resend','alternative'}:
        key=decision['product_id']
        a['fan_state']['pending_products']=sorted(set(a['fan_state']['pending_products'])|{key})
        a['fan_state']['previously_sent_assets']=sorted(set(a['fan_state']['previously_sent_assets'])|
            set(a['inventory'][key]['media_ids']))
    old_revision=a['control'].pop('input_revision');new_revision=b['control'].pop('input_revision')
    return new_revision==old_revision+1 and a==b
