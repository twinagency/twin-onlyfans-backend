"""One durable Moxy decision, ordered through the existing one-use intents."""
import json
import re
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


# ---- Repetition: substantial reused wording, not common words, pet names or emoji.
def _squash(word):
    return re.sub(r'(.)\1+',r'\1',word)          # babyyy -> baby, heyyy -> hey

PET_NAMES={_squash(w) for w in ('babe','baby','bby','bb','babes','babyy','bestie','bestiee','daddy','handsome','honey','hun','sweetie',
    'cutie','love','papi','boo','darling','sexy')}
COMMON={_squash(w) for w in ('i','you','me','my','your','the','a','an','and','to','of','it','is','are','am','was','in','on','at','for','so',
    'do','did','what','how','that','this','with','just','be','im','u','ur','we','can','if','or','but','not','no','yes','ok','okay','lol','omg','hey','hi')}


def _tokens(text):
    words=re.sub(r"[^\w\s']",' ',str(text).lower().replace('\u2019',"'")).split()
    return [w for w in (_squash(x.replace("'",'')) for x in words) if w and w not in PET_NAMES]


def _content(tokens):
    return sum(1 for t in tokens if t not in COMMON)


def shared_wording(new_text, old_text):
    """True when new_text reuses a substantial phrase of old_text.

    A shared run of 5 or more words that carries real content, or the same opening of 4 or more words
    in two longer messages (the same joke with a different ending). Pet names, common words and emoji
    never count, so ordinary vocabulary stays usable.
    """
    a=_tokens(new_text);b=_tokens(old_text)
    if len(a)<4 or len(b)<4:return False
    best=0;best_start=0;prefix=0
    previous=[0]*(len(b)+1)
    for i in range(1,len(a)+1):
        row=[0]*(len(b)+1)
        for j in range(1,len(b)+1):
            if a[i-1]==b[j-1]:
                row[j]=previous[j-1]+1
                if row[j]>best:best=row[j];best_start=i-row[j]
        previous=row
    while prefix<min(len(a),len(b)) and a[prefix]==b[prefix]:prefix+=1
    if best>=5 and _content(a[best_start:best_start+best])>=2:return True
    return prefix>=4 and min(len(a),len(b))>=6 and _content(a[:prefix])>=2


def reused_wording(parts, recent_texts):
    """The first recent line whose wording a new part substantially reuses, else None."""
    for part in parts:
        for old in recent_texts:
            if shared_wording(part['text'],old):return old
    return None


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
    # Offer eligibility is derived from fan_state/products (compared below); our own card changes it.
    a.pop('offer_eligibility',None);b.pop('offer_eligibility',None)
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
