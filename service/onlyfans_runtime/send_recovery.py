"""Audited positive receipt reconciliation through the existing delivery authority."""
import json
from sqlalchemy.orm import Session
from db.onlyfans_delivery_models import OfTextIntent
from .store import Refused,utcnow
from .text_delivery import Accepted
from .launch_operations import command_identity,digest,_prior,_record


def snapshot(authority,tenant,account,fan,intent_id):
    with Session(authority.engine) as s,s.begin():
        runtime=authority.store._runtime(s,tenant,account)
        row=authority._intent(s,account,intent_id)
        if (row.fan_id!=fan or row.binding_id!=runtime.binding_id or row.status not in ("dispatching","unknown","accepted")
            or row.wire_version!=1 or row.wire_started_at is None or not row.dispatch_token or not row.attempt_id
            or authority._intent_hash(row)!=row.payload_hash):
            raise Refused("recovery intent unavailable")
        return {"version":1,"account_id":account,"fan_id":fan,"binding_id":runtime.binding_id,
            "body":row.body,"content":{"media_ids":json.loads(row.media_json),"preview_ids":json.loads(row.preview_json),
                "price_cents":row.price_cents,"media_types":{str(f["media_id"]):f["kind"]
                    for f in json.loads(row.catalog_snapshot_json)["files"]}} if row.catalog_item_id is not None else None,
            "created_at":row.dispatched_at.isoformat(),"intent_id":row.intent_id,"attempt_id":row.attempt_id,
            "payload_hash":row.payload_hash,"token":row.dispatch_token}


def reconcile(authority,tenant,account,fan,intent_id,*,history,complete,provider_message_id,command_key,actor,reason):
    from native_pilot.recovery import _matches,proof_hash
    intent=snapshot(authority,tenant,account,fan,intent_id)
    if complete is not True or type(history) is not list or len(history)>1000:
        raise Refused("complete bounded recovery evidence required")
    matches=[]
    for raw in history:
        try:event=_matches(raw,intent)
        except Exception:continue
        matches.append((event,raw))
    if len(matches)!=1 or type(provider_message_id) is not int or matches[0][0].message_id!=provider_message_id:
        raise Refused("positive recovery proof unavailable")
    event,raw=matches[0]
    evidence_hash=proof_hash(raw,intent)
    request_hash=digest([intent_id,intent["attempt_id"],intent["payload_hash"],evidence_hash,actor,reason])
    command_identity(command_key,actor,reason,request_hash)
    # Audit the requested proof before applying it. A crash can replay this same
    # command through immutable receipt recording and idempotent projection.
    with Session(authority.engine) as s,s.begin():
        runtime=authority.store._runtime(s,tenant,account)
        row=authority._intent(s,account,intent_id)
        if runtime.binding_id!=intent["binding_id"] or row.attempt_id!=intent["attempt_id"] or row.payload_hash!=intent["payload_hash"]:
            raise Refused("recovery intent changed")
        if _prior(s,runtime,command_key,"send_reconciliation",request_hash) is None:
            _record(s,runtime,command_key,"send_reconciliation",request_hash,actor,reason,
                {"intentId":intent_id,"attemptId":intent["attempt_id"],"status":row.status,"providerObservation":raw},
                {"recorded":True,"intentId":intent_id,"evidenceHash":evidence_hash,"resumed":False},utcnow())
    proof=Accepted(account,fan,intent_id,intent["attempt_id"],intent["payload_hash"],provider_message_id,event.created_at,
        "operator-history:"+evidence_hash)
    outcome=authority.record_accepted(tenant,account,intent_id,intent["token"],proof)
    projected=authority.materialize(tenant,account,intent_id) if outcome=="accepted" else False
    return {"recorded":True,"outcome":outcome,"projected":projected,"intentId":intent_id,"resumed":False}
