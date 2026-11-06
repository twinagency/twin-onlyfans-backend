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



def repetition_texts(snapshot,decision):
    """Only unsupported/ordinary text is screened for conversational looping.

    Protect the exact evidenced span, never a whole reply merely because it
    starts with an apology or contains a quote. Factual validation still runs
    on the complete original text before these repetition-only projections.
    """
    from .conversation import clauses,quotes,_norm,_RETRACT,_ATTRIBUTION
    history=snapshot.get('history',[])
    from .conversation import pending_messages
    pending=pending_messages(snapshot)
    requests=' '.join(m.get('text','') for m in pending)
    correcting=bool(re.search(r"when did i say|i (?:never|didn'?t|did not) say|what did (?:you|i) say|that'?s not what|wrong|you said|quote|repeat",requests,re.I))
    factual_request=bool(re.search(r"\b(?:repeat|again|remind|confirm|how much|price|cost|how long|duration|minutes?|seconds?)\b",requests,re.I))
    previous=[m.get('text','') for m in history if m.get('direction')=='out']
    price_cents={t.get('price_cents') for t in snapshot.get('inventory',{}).values()}
    price_cents.update(c.get('price_cents') for c in snapshot.get('card_state',[]))
    facts=' '.join(f for p in snapshot.get('products',{}).values() for a in p.get('assets',[]) for f in a.get('facts',[]))
    numeric=re.compile(r"\$\s*(\d+(?:\.\d{1,2})?)|\b(\d+(?:\.\d{1,2})?)\s*(dollars?|usd|minutes?|mins?|seconds?|secs?)\b",re.I)
    def grounded_number(clause):
        values=list(numeric.finditer(clause))
        if not values:
            words=re.search(r'\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty|sixty)\s+(?:minutes?|seconds?)\b',clause,re.I)
            if not words or _norm(words.group()) not in _norm(facts):return False
        for match in values:
            amount=match.group(1) or match.group(2);unit=(match.group(3) or 'usd').lower()
            if unit in {'usd','dollar','dollars'}:
                from decimal import Decimal
                if int(Decimal(amount)*100) not in price_cents:return False
            else:
                stem='min' if unit.startswith('min') else 'sec'
                if not re.search(r'(?<![\d.])'+re.escape(amount)+r'\s*'+stem+r'(?:ute|ond)?s?\b',facts,re.I):return False
        # A factual answer is not a licence for an unrelated repeated pitch.
        return not re.search(r"\b(?:buy|unlock|purchase|open|send|want|wanna|go get|treat yourself)\b",clause,re.I)
    result=[]
    all_text=' '.join(p['text'] for p in plan(decision))
    owns_statement=bool(re.search(r"\bi (?:said|wrote|told|was the one)|\b(?:that|it) was me|\bmy (?:words|line|statement|mistake)\b",all_text,re.I))
    for part in plan(decision):
        kept=[]
        for clause in clauses(part['text']):
            # A retraction must quote/refer to actual previous outgoing words.
            if _RETRACT.search(clause):
                stripped=re.split(r"\b(?:said|claimed|saying)\b",clause,maxsplit=1,flags=re.I)
                if len(stripped)==2 and any(_norm(stripped[1]) and _norm(stripped[1]) in _norm(old) for old in previous):continue
            denial=re.search(r"\byou (?:didn'?t|did not|never) (?:say|tell me|mention|write) (.+)",clause,re.I)
            if denial and owns_statement:
                stop={'you','your','i','me','my','would','will','did','do','not','never','on','a','the','that','it'}
                needed=set(_norm(denial.group(1)).split())-stop
                # The original creator utterance must support the correction;
                # another bot claim about the fan is not its source evidence.
                if len(needed)>=2 and any(not _ATTRIBUTION.search(m.get('text','')) and needed<=set(_norm(m.get('text','')).split()) for m in history if m.get('direction')=='out'):continue
            if factual_request and grounded_number(clause):continue
            spans=quotes(clause)
            if correcting or _RETRACT.search(clause):
                for a,b,quote in reversed(spans):
                    if any(_norm(quote) and _norm(quote) in _norm(m.get('text','')) for m in history):clause=clause[:a]+clause[b:]
            kept.append(clause)
        result.append(' '.join(kept))
    return result

def index(event):
    progress=json.loads(event.payload_json).get('_moxy_delivery',{})
    return len(progress.get('receipts',[]))


def active(event):
    return emission(json.loads(event.decision_json),index(event))


def emission(original,position):
    """Project only wire/action effects; the immutable plan owns claim context."""
    values=plan(original)
    if type(position) is not int or position<0:raise ActionBlocked('invalid_message_position')
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


def continuation_unchanged(before, after, own_message_id, decision, *, account_id=None, session=None):
    """Permit only our just-reconciled message; external changes cancel the tail."""
    a=deepcopy(before);b=deepcopy(after)
    a.pop('native_fingerprint',None);b.pop('native_fingerprint',None)
    # Derived from history/fan_state, which are compared below; own append changes it.
    a.pop('commercial_state',None);b.pop('commercial_state',None)
    # Offer eligibility is derived from fan_state/products (compared below); our own card changes it.
    a.pop('offer_eligibility',None);b.pop('offer_eligibility',None)
    old=a.pop('history');new=b.pop('history')
    # The same accepted card can enter the all-history card evidence projection.
    # Only this receipt is exempted; changes to older purchase facts cancel the tail.
    old_cards=a.pop('card_state',[]);new_cards=b.pop('card_state',[])
    if [c for c in new_cards if c['id']!=own_message_id]!=old_cards:return False
    filtered=[m for m in new if m['id']!=own_message_id]
    # Snapshot history is bounded at 100. One own append may evict one oldest row.
    if filtered!=old[-len(filtered):] or not any(m['id']==own_message_id for m in new):return False
    # Our append can move exactly one unchanged source from recent history
    # into the additive historical-memory projection. That is not external
    # input. Every canonical fact, correction and retained excerpt stays fenced.
    evicted=old[:len(old)-len(filtered)]
    if evicted and session is not None:
        # Even a source which stops matching memory retrieval must not change
        # invisibly as it leaves the recent window during our own append.
        if len(evicted)!=1 or not _evicted_source_current(
                session,account_id,before.get('fan_id'),evicted[0]):return False
    if a.get('memory')!=b.get('memory'):
        if len(evicted)!=1 or account_id is None or not _own_memory_shift(
                a.get('memory'),b.get('memory'),evicted[0],account_id,before.get('fan_id'),session):
            return False
        b['memory']=deepcopy(a['memory'])
    if decision['action'] in {'offer','continue','resend','alternative'}:
        key=decision['product_id']
        a['fan_state']['pending_products']=sorted(set(a['fan_state']['pending_products'])|{key})
        a['fan_state']['previously_sent_assets']=sorted(set(a['fan_state']['previously_sent_assets'])|
            set(a['inventory'][key]['media_ids']))
    old_revision=a['control'].pop('input_revision');new_revision=b['control'].pop('input_revision')
    return new_revision==old_revision+1 and a==b



def _own_memory_shift(before,after,evicted,account,fan,session=None):
    if not isinstance(before,dict) or not isinstance(after,dict):return False
    from datetime import datetime
    from types import SimpleNamespace
    from onlyfans_runtime.launch_history_memory.extraction import source_digest
    from onlyfans_runtime.launch_history_memory.memory import _hash
    a=deepcopy(before);b=deepcopy(after)
    # Validate the projection's own digest before normalising its movement.
    for memory in (a,b):
        fingerprint=memory.pop('fingerprint',None)
        if fingerprint!=_hash(memory):return False
    old=a.pop('interactions',[]);new=b.pop('interactions',[])
    if a!=b:return False
    source=SimpleNamespace(account_id=account,fan_id=fan,message_id=evicted['id'],
        body=evicted.get('text') or '',direction=evicted['direction'],
        created_at=datetime.fromisoformat(evicted['at']),is_unsent=False)
    added=[x for x in new if x.get('sourceMessageId')==str(source.message_id)]
    if len(added)!=1:return False
    item=added[0]
    if (item.get('sourceDigest')!=source_digest(source)
            or item.get('sourceAt')!=source.created_at.isoformat()
            or item.get('direction')!=source.direction
            or not item.get('excerpt') or item['excerpt'] not in source.body):
        return False
    retained=[x for x in new if x is not item]
    # Top-k retrieval may evict its last excerpt, but cannot rewrite/reorder
    # other sources or conceal a changed canonical fact.
    if not (len(old)-1<=len(retained)<=len(old) and retained==old[:len(retained)]):return False
    if len(retained)<len(old):
        # A top-k eviction may not hide a concurrent edit/unsend of its old
        # last source. Verify that source against the existing canonical row.
        if session is None:return False
        from sqlalchemy import select
        from db.models import Message
        item=old[-1]
        row=session.scalar(select(Message).where(Message.account_id==account,Message.fan_id==fan,
            Message.message_id==int(item['sourceMessageId'])).with_for_update(read=True))
        if row is None or row.is_unsent or source_digest(row)!=item['sourceDigest']:return False
    return True



def _evicted_source_current(session,account,fan,source):
    from sqlalchemy import select
    from db.models import Message
    from datetime import datetime
    row=session.scalar(select(Message).where(Message.account_id==account,Message.fan_id==fan,
        Message.message_id==source['id']).with_for_update(read=True))
    return bool(row is not None and not row.is_unsent and row.body==(source.get('text') or '')
        and row.direction==source['direction'] and row.created_at==datetime.fromisoformat(source['at'])
        and row.media_count==source.get('media_count',0)
        and row.price_cents==source.get('price_cents',0)
        and row.image_desc==source.get('attachment_description'))
