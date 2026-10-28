"""Repair representation-only intake conflicts without rewriting either observation.

The raw ingress payload, its event key, and the existing message/receipt remain
unchanged. Scope and evidence are checked again under the normal account lock.
Substantive edits, missing media, old bindings and identity differences stay held.
"""
import hashlib
import json
from sqlalchemy import select
from sqlalchemy.orm import Session
from db.models import Message, VaultSend
from db.onlyfans_models import OfIngress
from .store import Event, Refused, utcnow
from .native_protocol import history_text
from .launch_operations import command_identity, digest, _prior, _record

def equivalent(message, event, *, saved_media=()):
    if (message.account_id is None or message.fan_id != event.fan_id or message.message_id != event.message_id
            or message.direction != event.direction or message.price_cents != event.price_cents):
        return False
    try:
        media = json.loads(message.media_ids or '[]')
        if media != list(event.media_ids):
            # Older pack sends saved exact file identities only in vault_sends.
            if media or not saved_media or list(saved_media) != list(event.media_ids):return False
        # Decode only provider HTML, never whitespace, punctuation or plain text entities.
        body = history_text(message.body) if message.body.startswith('<p>') and message.body.endswith('</p>') else message.body
        return body == event.body
    except (ValueError, TypeError, AttributeError):
        return False

def saved_media(s, message):
    if message is None or message.direction != 'out':return ()
    try:
        if json.loads(message.media_ids or '[]'):return ()
    except (ValueError,TypeError):return ()
    return tuple(s.scalars(select(VaultSend.media_id).where(VaultSend.account_id==message.account_id,
        VaultSend.fan_id==message.fan_id,VaultSend.message_id==message.message_id).order_by(VaultSend.id).limit(101)))

def _rows(s, runtime, fan):
    result=[]
    for ingress in s.scalars(select(OfIngress).where(OfIngress.account_id==runtime.account_id,
            OfIngress.fan_id==fan,OfIngress.status=='quarantined').order_by(OfIngress.event_key).limit(1001)):
        if len(result)>=1000: raise Refused('repair scope too large')
        message=s.get(Message,(runtime.account_id,fan,ingress.message_id))
        event=None
        try: event=Event(**json.loads(ingress.payload)).normalized()
        except (ValueError,TypeError): pass
        verified=bool(event and message and ingress.binding_id==runtime.binding_id
            and ingress.reason=='conflicting_message_identity' and event.fan_id==fan
            and event.message_id==ingress.message_id and equivalent(message,event,saved_media=saved_media(s,message)))
        evidence={'eventKey':ingress.event_key,'payloadHash':hashlib.sha256(ingress.payload.encode()).hexdigest(),
            'messageId':ingress.message_id,'messageHash':digest([message.direction,message.body,message.price_cents,message.media_ids]) if message else None,
            'reason':ingress.reason,'savedMediaIds':list(saved_media(s,message)),'verified':verified}
        result.append((ingress,evidence))
    return result

def inspect(store,tenant,account,fan):
    with Session(store.engine) as s,s.begin():
        runtime=store._runtime(s,tenant,account)
        evidence=[e for _,e in _rows(s,runtime,fan)]
        return {'fingerprint':digest([runtime.binding_id,fan,evidence]),'records':evidence}

def repair(store,tenant,account,fan,*,command_key,actor,reason,expected_fingerprint):
    command_identity(command_key,actor,reason,expected_fingerprint)
    request_hash=digest([fan,expected_fingerprint,actor,reason])
    with Session(store.engine) as s,s.begin():
        runtime=store._runtime(s,tenant,account)
        prior=_prior(s,runtime,command_key,'history_representation_repair',request_hash)
        if prior is not None:return prior
        rows=_rows(s,runtime,fan);evidence=[e for _,e in rows]
        if digest([runtime.binding_id,fan,evidence])!=expected_fingerprint:raise Refused('repair evidence changed')
        repaired=[]
        for ingress,item in rows:
            if item['verified']:
                ingress.status='applied';ingress.reason=None
                repaired.append(item['eventKey'])
        # No pending counter change: these rows already left the pending queue.
        # No automation resume, attention clear, message rewrite or provider send.
        result={'recorded':True,'repaired':len(repaired),'retained':len(rows)-len(repaired),'eventKeys':repaired}
        return _record(s,runtime,command_key,'history_representation_repair',request_hash,actor,reason,
            {'fanId':fan,'evidence':evidence},result,utcnow())
