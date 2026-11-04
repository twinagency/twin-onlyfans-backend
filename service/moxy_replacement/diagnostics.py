"""Private, bounded repair evidence, deliberately outside conversation fingerprints."""
import json
import os
import re
import uuid
from sqlalchemy import select
from db.onlyfans_moxy_models import MoxyAudit
from onlyfans_runtime.native_context import canonical
from onlyfans_runtime.store import utcnow

ACTOR = 'moxy-reply-diagnostic'


def redact(value, limit=20000):
    text = str(value)
    text = re.sub(r'(?i)\bBearer\s+\S+', 'Bearer [REDACTED]', text)
    text = re.sub(r'\b(?:sk|sk-proj|sk-or-v1|ghp|gho)[_-][A-Za-z0-9_-]{12,}', '[REDACTED]', text)
    text = re.sub(r'(?i)([?&](?:key|token|secret|signature)=)[^&\s"\']+', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)(["\']?(?:api_key|access_token|password|secret)["\']?\s*[:=]\s*["\']?)[^\s,"\'}]+',
                  r'\1[REDACTED]', text)
    return text[:limit]


def record(s, account, phase, *, event=None, raw=None, **data):
    evidence = {'phase': phase, 'release': os.environ.get('MOXY_RELEASE_SHA', 'unreported'), **data}
    if event is not None:
        payload = json.loads(event.payload_json)
        evidence.update(event_id=event.event_id, fan_id=event.fan_id, model_call_id=event.model_call_id,
                        input_message_id=payload.get('source_message_id'), correction_of=payload.get('correction_of'),
                        status=event.status, reason=event.reason, snapshot_hash=event.snapshot_hash)
    if raw is not None:
        evidence.update(raw=redact(raw), raw_bytes=len(str(raw).encode()),
                        raw_complete=len(str(raw).encode()) <= 20000 and redact(raw) == raw)
    def scrub(value):
        if isinstance(value,dict):return {k:scrub(v) for k,v in value.items()}
        if isinstance(value,list):return [scrub(v) for v in value]
        return redact(value) if isinstance(value,str) else value
    evidence=scrub(evidence)
    while len(canonical(evidence).encode())>48000 and evidence.get('raw'):
        evidence['raw']=evidence['raw'][:len(evidence['raw'])//2]
        evidence['raw_complete']=False
    identity=(str(uuid.uuid5(uuid.NAMESPACE_URL,'moxy:provider_result:'+account+':'+event.event_id+':'+str(event.model_call_id)))
        if phase=='provider_result' and event is not None else str(uuid.uuid4()))
    if s.get(MoxyAudit,identity) is not None:return
    s.add(MoxyAudit(audit_id=identity, account_id=account, actor=ACTOR,
                   before_json='{}', after_json=canonical(evidence), created_at=utcnow()))


def completion(s, event):
    identity=str(uuid.uuid5(uuid.NAMESPACE_URL,'moxy:provider_result:'+event.account_id+':'+event.event_id+':'+str(event.model_call_id)))
    direct=s.get(MoxyAudit,identity)
    if direct is not None and direct.actor==ACTOR:
        data=json.loads(direct.after_json)
        if data.get('phase')=='provider_result' and data.get('model_call_id')==event.model_call_id:return data
    rows = s.scalars(select(MoxyAudit).where(MoxyAudit.account_id == event.account_id,
        MoxyAudit.actor == ACTOR,MoxyAudit.after_json.like('%'+event.event_id+'%')).order_by(MoxyAudit.created_at.desc()))
    for row in rows:
        try:
            data = json.loads(row.after_json)
        except (ValueError, TypeError):
            continue
        if (data.get('phase') == 'provider_result' and data.get('event_id') == event.event_id
                and data.get('model_call_id') == event.model_call_id):
            return data
    return None
