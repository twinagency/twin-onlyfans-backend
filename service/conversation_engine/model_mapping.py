"""Immutable, scoped provider-model mapping evidence in the existing launch audit."""
import json
import re
from hashlib import sha256
from onlyfans_runtime.launch_operations_models import OfLaunchCommand
from onlyfans_runtime.store import Refused


def _encoded(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"))


def _hash(value):
    return sha256(_encoded(value).encode()).hexdigest()


def scope_key(account,binding,revision,fingerprint,endpoint,model):
    return _hash(["plan-c-model-canonical-scope",account,binding,revision,fingerprint,endpoint,model])


def call_key(account,call_id):
    return _hash(["plan-c-model-canonical-call",account,call_id])


def scope_pin(s,account,binding,revision,fingerprint,endpoint,model):
    key=scope_key(account,binding,revision,fingerprint,endpoint,model)
    row=s.get(OfLaunchCommand,(account,key))
    if row is None:return None
    try:
        data=json.loads(row.before_json)
        expected={"account":account,"binding":binding,"scopeRevision":revision,"scopeFingerprint":fingerprint,
                  "endpointSha256":endpoint,"requestedModel":model}
        if (row.kind!="plan_c_model_canonical_pin" or row.binding_id!=binding or
                any(data.get(k)!=v for k,v in expected.items()) or row.request_hash!=_hash(data) or
                type(data.get("canonicalModel")) is not str or
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}",data["canonicalModel"]) or
                type(data.get("mappingReceiptSha256")) is not str or
                not re.fullmatch(r"[a-f0-9]{64}",data["mappingReceiptSha256"])):
            raise ValueError("invalid mapping")
    except (TypeError,ValueError,AttributeError,json.JSONDecodeError):
        raise Refused("stored model mapping evidence is invalid") from None
    return row,data


def capture_call_pin(s,row,pilot_stamp,endpoint,now):
    """Snapshot an already committed mapping atomically with a new reservation."""
    if not pilot_stamp:return
    found=scope_pin(s,row.account_id,row.binding_id,pilot_stamp[0],pilot_stamp[1],endpoint,row.model)
    if found is None:return
    pin,mapping=found
    data={**mapping,"callId":row.call_id,"scopePinKey":pin.command_key}
    s.add(OfLaunchCommand(account_id=row.account_id,command_key=call_key(row.account_id,row.call_id),
        binding_id=row.binding_id,kind="plan_c_model_call_pin",request_hash=_hash(data),actor=pin.actor,
        reason="Provider canonical mapping captured at model reservation",
        before_json=_encoded(data),result_json="{}",created_at=now))


def call_pin(s,row,endpoint):
    """A later scope pin cannot retrofit evidence onto an earlier call."""
    audit=s.get(OfLaunchCommand,(row.account_id,call_key(row.account_id,row.call_id)))
    if audit is None:return None
    try:
        data=json.loads(audit.before_json)
        if (audit.kind!="plan_c_model_call_pin" or audit.binding_id!=row.binding_id or
                audit.request_hash!=_hash(data) or data.get("callId")!=row.call_id or
                data.get("account")!=row.account_id or data.get("binding")!=row.binding_id or
                data.get("scopeRevision")!=row.pilot_scope_revision or
                data.get("scopeFingerprint")!=row.pilot_scope_fingerprint or
                data.get("endpointSha256")!=endpoint or data.get("requestedModel")!=row.model):
            raise ValueError("call mapping mismatch")
        found=scope_pin(s,row.account_id,row.binding_id,row.pilot_scope_revision,
                        row.pilot_scope_fingerprint,endpoint,row.model)
        if found is None or data.get("scopePinKey")!=found[0].command_key:
            raise ValueError("scope mapping missing")
        if {k:v for k,v in data.items() if k not in ("callId","scopePinKey")}!=found[1]:
            raise ValueError("scope mapping differs")
    except (TypeError,ValueError,AttributeError,json.JSONDecodeError):
        raise Refused("stored call mapping evidence is invalid") from None
    return data
