"""Audited positive receipt reconciliation through the existing delivery authority."""
import json
from sqlalchemy.orm import Session
from db.onlyfans_delivery_models import OfTextIntent
from .store import Refused,utcnow
from .text_delivery import Accepted
from .launch_operations import command_identity,digest,_prior,_record


RETIRED_UNCONFIRMED_TEXT = "operator_retired_unconfirmed_text"


def snapshot(authority,tenant,account,fan,intent_id):
    with Session(authority.engine) as s,s.begin():
        runtime=authority.store._runtime(s,tenant,account)
        row=authority._intent(s,account,intent_id)
        retired = row.status == "cancelled" and row.reason == RETIRED_UNCONFIRMED_TEXT
        if (row.fan_id!=fan or row.binding_id!=runtime.binding_id or
            (row.status not in ("dispatching","unknown","accepted") and not retired)
            or row.wire_version!=1 or row.wire_started_at is None or not row.dispatch_token or not row.attempt_id
            or authority._intent_hash(row)!=row.payload_hash):
            raise Refused("recovery intent unavailable")
        return {"version":1,"account_id":account,"fan_id":fan,"binding_id":runtime.binding_id,
            "body":row.body,"content":{"media_ids":json.loads(row.media_json),"preview_ids":json.loads(row.preview_json),
                "price_cents":row.price_cents,"media_types":{str(f["media_id"]):f["kind"]
                    for f in json.loads(row.catalog_snapshot_json)["files"]}} if row.catalog_item_id is not None else None,
            "created_at":row.dispatched_at.isoformat(),"intent_id":row.intent_id,"attempt_id":row.attempt_id,
            "payload_hash":row.payload_hash,"token":row.dispatch_token}


def reconcile(authority,tenant,account,fan,intent_id,*,history,complete,provider_message_id,command_key,actor,reason,expected_revision=None):
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
            if expected_revision is not None:
                from db.models import OfConversationState
                state=s.get(OfConversationState,(account,fan))
                if not state or state.revision!=expected_revision or state.unresolved_attempt_id!=intent['attempt_id']:
                    raise Refused('control revision conflict')
            _record(s,runtime,command_key,"send_reconciliation",request_hash,actor,reason,
                {"intentId":intent_id,"attemptId":intent["attempt_id"],"status":row.status,"providerObservation":raw},
                {"recorded":True,"intentId":intent_id,"evidenceHash":evidence_hash,"resumed":False},utcnow())
    proof=Accepted(account,fan,intent_id,intent["attempt_id"],intent["payload_hash"],provider_message_id,event.created_at,
        "operator-history:"+evidence_hash)
    outcome=authority.record_accepted(tenant,account,intent_id,intent["token"],proof)
    projected=authority.materialize(tenant,account,intent_id) if outcome=="accepted" else False
    return {"recorded":True,"outcome":outcome,"projected":projected,"intentId":intent_id,"resumed":False}


def retire_unconfirmed_text(authority, tenant, account, fan, intent_id, *, expected_attempt_id,
                            expected_revision, next_input_id, history, complete, observed_at,
                            command_key, actor, reason):
    """Retire one expired, unconfirmed pilot text while retaining its wire record.

    This is an operator decision, not a provider rejection. A newer verified
    inbound can proceed through the normal worker; the old draft cannot replay.
    """
    from datetime import timedelta
    from sqlalchemy import select
    from db.models import Fan, Message, OfConversationState
    from db.onlyfans_delivery_models import OfDeliveryPolicy
    from db.onlyfans_models import OfWork
    from db.onlyfans_moxy_models import MoxyAccount, MoxyEvent
    from .native_protocol import message
    from native_pilot.scope import AccountScope

    request_hash = digest([intent_id, expected_attempt_id, expected_revision, fan, next_input_id, actor, reason])
    command_identity(command_key, actor, reason, request_hash)
    with Session(authority.engine) as s, s.begin():
        runtime = authority.store._runtime(s, tenant, account)
        prior = _prior(s, runtime, command_key, "retire_unconfirmed_text", request_hash)
        if prior is not None:
            return prior
        now = utcnow()
        if complete is not True or type(history) is not list or not 1 <= len(history) <= 1000:
            raise Refused("complete provider history required")
        if observed_at.tzinfo is not None or not now - timedelta(minutes=2) <= observed_at <= now:
            raise Refused("fresh provider history required")
        owner = s.get(MoxyAccount, account)
        policy = s.get(OfDeliveryPolicy, account)
        row = authority._intent(s, account, intent_id)
        if (owner is None or owner.mode != "off" or owner.binding_id != runtime.binding_id
                or json.loads(owner.settings_json).get("pilot", {}).get("fan_ids") != [fan]
                or policy is None or policy.transport_kind != "native" or policy.delivery_mode != "gap"
                or policy.active_intent_id != intent_id or not policy.enabled):
            raise Refused("paused single-fan native pilot required")
        if (row.fan_id != fan or row.binding_id != runtime.binding_id or row.attempt_id != expected_attempt_id
                or row.status != "unknown" or row.reason != "ambiguous_transport" or row.origin != "automated"
                or row.price_cents != 0 or row.catalog_item_id is not None or row.offer_id is not None
                or row.quote_json or json.loads(row.media_json) or json.loads(row.preview_json)
                or row.receipt_json is not None or row.provider_message_id is not None
                or row.wire_version != 1 or row.wire_started_at is None or not row.dispatch_token
                or row.expires_at > now or row.deadline > now or authority._intent_hash(row) != row.payload_hash):
            raise Refused("expired unconfirmed Moxy text required")
        original = s.get(MoxyEvent, (account, row.moxy_event_id))
        state = s.get(OfConversationState, (account, fan))
        work = s.get(OfWork, (account, fan))
        latest = s.scalar(select(Message).where(
            Message.account_id == account, Message.fan_id == fan, Message.is_unsent.is_(False),
        ).order_by(Message.created_at.desc(), Message.message_id.desc()).limit(1))
        if (original is None or original.fan_id != fan or original.kind not in {"turn", "welcome"}
                or original.status != "queued" or original.intent_id != intent_id
                or state is None or state.revision != expected_revision or state.mode != "automated"
                or state.takeover_state != "none" or state.unresolved_attempt_id != row.attempt_id
                or work is None or work.status != "blocked" or work.target_message_id != next_input_id
                or work.control_revision != state.revision or latest is None or latest.direction != "in"
                or latest.message_id != next_input_id or latest.created_at <= row.wire_started_at
                or s.get(Fan, (account, fan)).is_muted
                or authority._retained_hold(s, account, fan)
                or s.scalar(select(OfTextIntent.intent_id).where(
                    OfTextIntent.account_id == account, OfTextIntent.intent_id != intent_id,
                    OfTextIntent.status.in_(("dispatching", "unknown", "queued")),
                ).limit(1))):
            raise Refused("fresh scoped work unavailable or another hold exists")
        provider_owner = int(AccountScope.parse(account).provider_id)
        observations = [message(item, owner_id=provider_owner, fan_id=fan, history=True) for item in history]
        if (any(item.direction == "out" for item in observations)
                or not any(item.message_id == next_input_id and item.direction == "in" for item in observations)):
            raise Refused("provider history requires positive reconciliation or fresh inbound")
        before = {"intentId": intent_id, "attemptId": row.attempt_id, "status": row.status,
                  "eventId": original.event_id, "payloadHash": row.payload_hash,
                  "providerHistoryHash": digest(history), "providerObservedAt": observed_at.isoformat()}
        row.status, row.reason, row.updated_at = "cancelled", RETIRED_UNCONFIRMED_TEXT, now
        original.status, original.reason, original.updated_at = "stale", RETIRED_UNCONFIRMED_TEXT, now
        # Retain the reservation and every wire field. Release only this hold.
        authority._release_hold(s, row, tenant)
        work.generation += 1
        work.status, work.due_at = "waiting", max(work.due_at, now)
        work.lease_token = work.lease_until = None
        return _record(s, runtime, command_key, "retire_unconfirmed_text", request_hash, actor, reason, before,
            {"retired": True, "deliveryOutcome": "unconfirmed", "nextInputId": next_input_id}, now)
