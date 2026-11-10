"""Exact queue-evidence review helpers. Never classify queue events as harmless.

A reconciliation is an explicit decision by the existing authenticated reconcile
operator. The references/hashes attest to evidence they reviewed; they are not a
substitute for authenticated provider collection or permission to send.
"""
from datetime import datetime, timezone
from hashlib import sha256
import json
from .events import _parse
from onlyfans_runtime.store import Refused

QUEUE_EVENTS = frozenset(('chat_queue.updated', 'chat_queue.finished'))
_FIELDS = frozenset(('id','date','canUnsend','hasError','isCanceled','isCouplePeopleMedia',
                     'isDone','isReady','pending','total','unsendSeconds'))
_BOOLS = ('canUnsend','hasError','isCanceled','isCouplePeopleMedia','isDone','isReady')


def timestamp(value):
    if type(value) is not str or len(value)>64:
        raise ValueError('explicit UTC evidence date required')
    stamp=datetime.fromisoformat(value.replace('Z','+00:00'))
    if stamp.tzinfo is None or stamp.year<2000:
        raise ValueError('aware evidence date required')
    return stamp.astimezone(timezone.utc)


def queue_envelope(raw):
    body=_parse(raw)
    payload=body['payload']
    if body['event'] not in QUEUE_EVENTS or type(payload) is not dict or set(payload)!=_FIELDS:
        raise ValueError('only exact documented queue progress is reviewable')
    ident=payload['id']
    if type(ident) not in (str,int) or not str(ident).isascii() or not str(ident).isdecimal() or not 0<int(ident)<2**63 or str(int(ident))!=str(ident):
        raise ValueError('invalid queue identity')
    if any(type(payload[k]) is not bool for k in _BOOLS):
        raise ValueError('invalid queue flags')
    if (any(type(payload[k]) is not int or not 0<=payload[k]<2**63 for k in ('pending','unsendSeconds'))
            or type(payload['total']) is not int or not -1<=payload['total']<2**63
            or payload['total']==-1 and payload['pending']!=0
            or payload['total']!=-1 and payload['pending']>payload['total']):
        raise ValueError('invalid queue counts')
    timestamp(payload['date'])
    return body, int(ident)


def reviewed_queue_delivery(local, key, payload, provider):
    """Validate an exact DB-audited group for an interval collector. No outside reads.

    Collector supplies current immutable digests and queue-review audit rows from
    the same account/binding. A new digest invalidates the previous group proof.
    """
    digests={d['hash'] for d in local.get('digests',[]) if d.get('key')==key}
    if not digests:
        return False
    for audit in local.get('queue_reviews',[]):
        before, result=audit.get('before',{}),audit.get('result',{})
        if (before.get('eventKey')!=key or before.get('providerAccount')!=provider
                or result.get('outcome')!='queue_event_group_reviewed' or result.get('resolved') is not True
                or set(before.get('payloadSha256s',[]))!=digests):
            continue
        try:
            raw_bodies=before['rawBodies']
            if type(raw_bodies) is not list or not 1<=len(raw_bodies)<=16:
                continue
            seen=set();matched=False;queue_ids=set()
            for original in raw_bodies:
                raw=original.encode('utf-8');body,qid=queue_envelope(raw)
                digest=sha256(raw).hexdigest()
                if digest not in digests or digest in seen or body['account_id']!=provider:
                    raise ValueError('queue review changed')
                seen.add(digest);queue_ids.add(qid)
                matched=matched or body==payload
            if seen==digests and len(queue_ids)==1 and matched:
                return True
        except (ValueError,KeyError,TypeError,AttributeError,Refused):
            continue
    return False
