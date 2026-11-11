"""Audited positive receipt reconciliation through the existing delivery authority.

Also the one audited way to release a Plan C send whose provider history PROVES the message is absent
(release_unconfirmed_send); see the module-level notes there before changing anything about it.
"""
from dataclasses import dataclass
from datetime import datetime,timedelta
import difflib
import hashlib
import json
import re
from sqlalchemy import select
from sqlalchemy.orm import Session
from db.onlyfans_delivery_models import OfTextIntent
from .store import Refused,utcnow
from .text_delivery import Accepted
from .launch_operations import command_identity,digest,_prior,_record


RETIRED_UNCONFIRMED_TEXT = "operator_retired_unconfirmed_text"
RELEASED_UNCONFIRMED_SEND = "operator_released_unconfirmed_send"


def snapshot(authority,tenant,account,fan,intent_id):
    with Session(authority.engine) as s,s.begin():
        runtime=authority.store._runtime(s,tenant,account)
        row=authority._intent(s,account,intent_id)
        from conversation_engine.delivery import is_engine_intent
        retired = (row.status == "cancelled" and row.reason == RETIRED_UNCONFIRMED_TEXT
                   and not is_engine_intent(s, row))  # engine intents are one-use: never reopened
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
        from conversation_engine.pilot_scope import authorize_fan
        authorize_fan(s,runtime,fan,actor,'reconcile')
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
                {"intentId":intent_id,"attemptId":intent["attempt_id"],"status":row.status,"providerObservation":raw,
                    **_retained_send_evidence(s,row)},
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



def _retained_send_evidence(s,row):
    """{'sendEvidence': ...} when the engine attempt kept evidence about why the send is unknown, else {} (audit only)."""
    from db.onlyfans_engine_models import OfEngineAttempt
    if row.attempt_id is None:return {}
    attempt=s.get(OfEngineAttempt,(row.account_id,row.attempt_id))
    if attempt is None or attempt.evidence_json is None or attempt.outcome!="unknown":return {}
    try:value=json.loads(attempt.evidence_json)
    except ValueError:return {}
    return {"sendEvidence":value} if type(value) is dict and value.get("v")==1 else {}


# ======================================================================================================================
# Release of an unknown Plan C send that provider history PROVES is absent.
#
# retire_unconfirmed_text above is hard-wired to the paused single-fan NATIVE pilot. The FansAPI Plan C trial had no way
# to release a fan whose send ended unknown unless the exact message could be found (reconcile). This is the other half:
# only when a complete, fresh, guarded provider-history read shows NO message that could be ours, after a quiet period,
# with a newer fan message waiting, may an authorized operator release the HOLD (never the record):
#   * the intent becomes cancelled/"operator_released_unconfirmed_send": never claimed as not sent, wire fields and the
#     delivery-unit reservation stay (used_units never decreases), the engine decision is held, its request cancelled;
#   * every other hold (stop, no-contact, takeover, restriction, safety/operator case, pending evidence) still wins;
#   * if the history DOES show the message, reconcile() above is the only way and this refuses.
# ======================================================================================================================
QUIET_SECONDS=1800                       # default AND floor: a late provider-side delivery must have had time to show up
COVER_MARGIN=timedelta(seconds=60)       # provider/local clock slack before the dispatch that history must still cover
MATCH_WINDOW=timedelta(minutes=5)        # the window reconcile() uses; any unknown creator message in it is ambiguity
FRESH_READ=timedelta(minutes=2)          # the history read must have just finished
CLOCK_SKEW=timedelta(seconds=30)
MAX_HISTORY=1000
RELEASABLE_REASONS=("invalid_receipt","ambiguous_transport","dispatch_expired")   # never provider_identity_conflict (a receipt exists)


@dataclass(frozen=True)
class HistoryRead:
    """What read_absence_history() saw: only that function builds one, release_unconfirmed_send() re-checks all of it."""
    account_id:str
    fan_id:int
    creator_id:int
    messages:tuple
    complete:bool
    reached_start:bool
    pages:int
    started_at:datetime      # naive UTC, before the first request
    ended_at:datetime        # naive UTC, after the last response

    @property
    def digest(self):
        return hashlib.sha256(json.dumps(list(self.messages),sort_keys=True,separators=(",",":"),default=str).encode()).hexdigest()


def _stamp(raw):
    from fans_api_adapter.payment_evidence import stamp
    try:return stamp(raw)
    except Refused:return None


def _number(value):
    from fans_api_adapter.payment_evidence import numeric
    try:return numeric(value)
    except Refused:return None


def _guarded_creator(authority,tenant,account,fan,client):
    """The creator id this client reads for, after proving it carries the CURRENT pilot request guard for this fan."""
    from fans_api_adapter.protocol import FansApiClient
    from conversation_engine.pilot_scope import current,ProviderRequestGuard,StoppedBackfillGuard
    from db.onlyfans_engine_models import OfFansConnection
    if type(client) is not FansApiClient or not client._identity_verified:
        raise Refused("verified provider client required")
    with Session(authority.engine) as s,s.begin():
        runtime=authority.store._runtime(s,tenant,account)
        scope,config=current(s,runtime)
        guard=getattr(client,"_request_guard",None)
        if (scope is None or fan not in config["fans"]
                or not ((scope.status=="active" and type(guard) is ProviderRequestGuard)
                        or (scope.status=="stopped" and type(guard) is StoppedBackfillGuard))
                or guard.store is not authority.store or guard.tenant!=tenant or guard.account!=account
                or guard.revision!=scope.revision):
            raise Refused("history read lacks the current pilot request guard")
        connection=s.get(OfFansConnection,(account,client.epoch))
        if (connection is None or connection.status!="active" or connection.binding_id!=runtime.binding_id
                or connection.provider_account_id!=client.account or connection.provider_creator_id!=client.creator_id):
            raise Refused("provider connection changed")
        creator=_number(connection.provider_creator_id)
        if creator is None:raise Refused("provider connection changed")
        return creator


def read_absence_history(authority,tenant,account,fan,intent_id,*,client,max_pages=10):
    """Read this fan's newest history through the budgeted pilot request guard until it covers the send, or give up.

    Each page is one guarded provider request (counted against the pilot budget). The result is complete only when the
    provider reported the start of the chat, or an older message than (dispatch - COVER_MARGIN) was seen, within
    max_pages (<= 10 pages of 100). Anything else (unreadable page, disorder, budget end, no coverage) is not complete.
    """
    if type(max_pages) is not int or not 1<=max_pages<=10:raise Refused("invalid history bound")
    intent=snapshot(authority,tenant,account,fan,intent_id)
    creator=_guarded_creator(authority,tenant,account,fan,client)
    lower=datetime.fromisoformat(intent["created_at"])-COVER_MARGIN
    started=utcnow()
    messages,next_page,pages,reached,covered,previous=[],None,0,False,False,None
    for _ in range(max_pages):
        try:result=client.history(str(fan),limit=100,next_page=next_page)
        except ValueError as exc:raise Refused("provider history unavailable") from exc
        pages+=1
        if result.state!="observed" or type(result.data) not in (list,tuple):raise Refused("provider history unavailable")
        for raw in result.data:
            moment=_stamp(raw.get("createdAt")) if type(raw) is dict else None
            if moment is None:raise Refused("provider history unreadable")
            if previous is not None and moment>previous+CLOCK_SKEW:raise Refused("provider history out of order")
            previous=moment if previous is None else min(previous,moment)
            messages.append(raw)
            if moment<=lower:covered=True
        if result.next_page is None:
            reached=True
            break
        if covered:break
        next_page=result.next_page
    return HistoryRead(account,fan,creator,tuple(messages),bool(reached or covered),reached,pages,started,utcnow())


def _squash(text):
    return re.sub(r"[\W_]+"," ",text.casefold()).strip()


def _similar(a,b):
    """True when two texts could be the same message after provider formatting (equal, contained or nearly equal)."""
    squashed_a,squashed_b=_squash(a)[:4000],_squash(b)[:4000]
    if not squashed_a or not squashed_b:return a.strip()==b.strip()
    if squashed_a==squashed_b:return True
    short,long=sorted((squashed_a,squashed_b),key=len)
    if len(short)>=6 and short in long:return True
    matcher=difflib.SequenceMatcher(None,squashed_a,squashed_b,autojunk=False)
    return matcher.real_quick_ratio()>=0.85 and matcher.quick_ratio()>=0.85 and matcher.ratio()>=0.85


def _absence_findings(history,*,fan,creator,body,dispatched,known_ids,next_input_id,now):
    """None when history proves no message that could be ours exists; otherwise the (fixed-vocabulary) reason."""
    from .native_protocol import message,NativeProtocolError
    seen,next_seen=set(),False
    for raw in history.messages:
        if type(raw) is not dict:return "history_item_unreadable"
        ident=_number(raw.get("id"))
        moment=_stamp(raw.get("createdAt"))
        sender=raw.get("fromUser")
        sender=_number(sender.get("id")) if type(sender) is dict else None
        if ident is None or moment is None:return "history_item_unreadable"
        if ident in seen:return "history_duplicate_id"
        seen.add(ident)
        if moment>now+MATCH_WINDOW or moment>history.ended_at+MATCH_WINDOW:return "clock_skew"
        if sender==fan:
            if ident==next_input_id:
                try:event=message(raw,owner_id=creator,fan_id=fan,history=True)
                except NativeProtocolError:return "next_input_unreadable"
                next_seen=event.direction=="in"
            continue
        if moment<dispatched-COVER_MARGIN or ident in known_ids:continue      # earlier, or a message we know is another one
        if moment<=dispatched+MATCH_WINDOW:return "creator_message_in_send_window"
        if sender!=creator:continue
        try:event=message(raw,owner_id=creator,fan_id=fan,history=True)
        except NativeProtocolError:return "creator_message_unreadable"
        if event.direction=="out" and _similar(event.body,body):return "same_text_after_send"
    return None if next_seen else "next_input_not_in_history"


def release_unconfirmed_send(authority,tenant,account,fan,intent_id,*,expected_attempt_id,expected_revision,next_input_id,
                             history,command_key,actor,reason,quiet_seconds=QUIET_SECONDS):
    """Release the HOLD of one unknown Plan C plain-text send whose absence the provider's own history proves.

    Idempotent per command key (replay returns the first result without re-checking). Every refusal leaves the intent,
    the fan's hold, the reservation and the engine records exactly as they were. The actor needs the existing
    pilot 'reconcile' permission; the pilot scope must contain the fan.
    """
    if (type(fan) is not int or isinstance(fan,bool) or type(intent_id) is not str or type(expected_attempt_id) is not str
            or type(expected_revision) is not int or isinstance(expected_revision,bool)
            or type(next_input_id) is not int or isinstance(next_input_id,bool) or next_input_id<=0
            or type(quiet_seconds) is not int or isinstance(quiet_seconds,bool) or not QUIET_SECONDS<=quiet_seconds<=7*86400):
        raise Refused("invalid release command")
    request_hash=digest(["release_unconfirmed_send",intent_id,expected_attempt_id,expected_revision,fan,next_input_id,quiet_seconds,actor,reason])
    command_identity(command_key,actor,reason,request_hash)
    from db.models import Fan,Message,OfConversationState
    from db.onlyfans_delivery_models import OfDeliveryPolicy
    from db.onlyfans_engine_models import OfEngineAttempt,OfEngineDecision,OfEngineIndependentAdmission,OfEngineRequest,OfFansConnection
    from db.onlyfans_models import OfIngress
    from conversation_engine import pilot_scope
    from conversation_engine.controls import reason as hold_reason
    from conversation_engine.history_scope import current_binding_message
    from conversation_engine.independence import admission_for
    from .text_delivery import _attach_evidence,_iso
    quiet=timedelta(seconds=quiet_seconds)
    with Session(authority.engine) as s,s.begin():
        runtime=authority.store._runtime(s,tenant,account)
        prior=_prior(s,runtime,command_key,"release_unconfirmed_send",request_hash)
        if prior is not None:return prior
        scope,config=pilot_scope.current(s,runtime)
        if scope is None or scope.status not in ("active","stopped") or fan not in config["fans"]:
            raise Refused("fan is outside an active or stopped Plan C pilot scope")
        pilot_scope.authorize(config,actor,"reconcile")
        policy=s.scalar(select(OfDeliveryPolicy).where(OfDeliveryPolicy.account_id==account).with_for_update())
        if policy is None or policy.transport_kind!="fansapi":raise Refused("Plan C Fans API delivery required")
        now=authority._db_now(s)
        row=authority._intent(s,account,intent_id)
        attempt=s.get(OfEngineAttempt,(account,row.attempt_id)) if row.attempt_id else None
        decision=s.scalar(select(OfEngineDecision).where(OfEngineDecision.account_id==account,
            OfEngineDecision.intent_id==intent_id).with_for_update())
        request=s.get(OfEngineRequest,(account,decision.request_id),with_for_update=True) if decision is not None else None
        # 1. exactly one unknown, unsettled, plain-text engine reply of this fan, unchanged since it was sent
        if (row.fan_id!=fan or row.binding_id!=runtime.binding_id or row.attempt_id!=expected_attempt_id
                or row.status!="unknown" or row.reason not in RELEASABLE_REASONS or row.origin!="automated"
                or row.price_cents!=0 or row.catalog_item_id is not None or row.offer_id is not None or row.quote_json
                or json.loads(row.media_json) or json.loads(row.preview_json)
                or row.receipt_json is not None or row.provider_message_id is not None or row.projected_at is not None
                or row.wire_version!=1 or row.wire_started_at is None or row.dispatched_at is None
                or not row.dispatch_token or not row.reservation_held or authority._intent_hash(row)!=row.payload_hash):
            raise Refused("unconfirmed plain text send required")
        if (attempt is None or attempt.outcome!="unknown" or attempt.intent_id!=intent_id or attempt.payload_hash!=row.payload_hash
                or decision is None or decision.status!="unknown" or decision.action!="reply" or decision.fan_id!=fan
                or decision.binding_id!=runtime.binding_id or request is None or request.status!="waiting_provider"
                or admission_for(s,row) is not None):
            raise Refused("engine attempt is not an unknown plain reply")
        # 2. quiet period, measured from the LAST moment the attempt could still have been on the wire
        last=max(row.wire_started_at,row.dispatched_at,attempt.updated_at)
        if last>now+CLOCK_SKEW:raise Refused("clock skew: the send is dated in the future")
        if now-last<quiet:raise Refused("quiet period has not elapsed")
        # 3. complete, fresh, guarded history read AFTER the quiet period, covering the dispatch, by this creator
        connection=s.scalar(select(OfFansConnection).where(OfFansConnection.account_id==account,
            OfFansConnection.binding_id==runtime.binding_id,OfFansConnection.status=="active"))
        creator=_number(connection.provider_creator_id) if connection is not None else None
        if (type(history) is not HistoryRead or creator is None or history.account_id!=account or history.fan_id!=fan
                or history.creator_id!=creator or history.complete is not True or type(history.messages) is not tuple
                or len(history.messages)>MAX_HISTORY or history.pages<1):
            raise Refused("complete provider history required")
        if history.started_at<last+quiet:raise Refused("provider history was read before the quiet period ended")
        if not history.started_at<=history.ended_at<=now+CLOCK_SKEW or now-history.ended_at>FRESH_READ:
            raise Refused("fresh provider history required")
        dispatched=row.dispatched_at
        known=set(s.scalars(select(OfTextIntent.provider_message_id).where(OfTextIntent.account_id==account,
            OfTextIntent.provider_message_id.is_not(None))))
        oldest=min((_stamp(m.get("createdAt")) for m in history.messages if type(m) is dict and _stamp(m.get("createdAt")) is not None),default=None)
        if not history.reached_start and (oldest is None or oldest>dispatched-COVER_MARGIN):
            raise Refused("provider history does not cover the send")
        finding=_absence_findings(history,fan=fan,creator=creator,body=row.body,dispatched=dispatched,known_ids=known,
            next_input_id=next_input_id,now=now)
        if finding is not None:
            raise Refused("absence is not proven ("+finding+"); use positive reconciliation if the message was delivered")
        canonical_out=s.scalars(select(Message.message_id).where(Message.account_id==account,Message.fan_id==fan,
            Message.direction=="out",Message.created_at>=dispatched-COVER_MARGIN))
        if any(ident not in known for ident in canonical_out):
            raise Refused("absence is not proven (canonical outbound message present)")
        # 4. every other hold still wins
        state=s.get(OfConversationState,(account,fan))
        fan_row=s.get(Fan,(account,fan))
        if state is None or fan_row is None or state.revision!=expected_revision or state.unresolved_attempt_id!=row.attempt_id:
            raise Refused("control revision conflict")
        held=hold_reason(s,account,fan,state,fan_row,unresolved=False)
        if held is not None:raise Refused("conversation hold remains: "+held)
        blocked=pilot_scope.delivery_block(s,runtime,fan)
        if blocked is not None:raise Refused("pilot evidence hold remains: "+blocked)
        if (authority._retained_hold(s,account,fan,own_attempt_id=row.attempt_id)
                or s.scalar(select(OfTextIntent.intent_id).where(OfTextIntent.account_id==account,OfTextIntent.fan_id==fan,
                    OfTextIntent.intent_id!=intent_id,OfTextIntent.status.in_(("dispatching","unknown","queued"))).limit(1)) is not None
                or s.scalar(select(OfEngineIndependentAdmission.attempt_id).where(OfEngineIndependentAdmission.account_id==account,
                    OfEngineIndependentAdmission.anchor_intent_id==intent_id,
                    OfEngineIndependentAdmission.status.in_(("dispatching","unknown"))).limit(1)) is not None):
            raise Refused("another delivery hold exists for this fan")
        # 5. the next fresh fan message exists, is current, live and is not the message that was being answered
        latest=s.scalar(select(Message).where(Message.account_id==account,Message.fan_id==fan,Message.is_unsent.is_(False),
            current_binding_message(runtime.binding_id)).order_by(Message.created_at.desc(),Message.message_id.desc()).limit(1))
        live=s.scalar(select(OfIngress.event_key).where(OfIngress.account_id==account,OfIngress.fan_id==fan,
            OfIngress.message_id==next_input_id,OfIngress.binding_id==runtime.binding_id,OfIngress.status=="applied",
            OfIngress.source!="import").limit(1))
        if (latest is None or latest.direction!="in" or latest.message_id!=next_input_id or latest.created_at<=row.wire_started_at
                or next_input_id==request.source_message_id or live is None):
            raise Refused("a fresh fan message after the unconfirmed send is required")
        # --- apply: release the hold, keep the record
        evidence=_retained_send_evidence(s,row)
        before={"intentId":intent_id,"attemptId":row.attempt_id,"status":row.status,"reason":row.reason,"payloadHash":row.payload_hash,
            "dispatchedAt":_iso(row.dispatched_at),"wireStartedAt":_iso(row.wire_started_at),"lastActivityAt":_iso(last),
            "quietSeconds":quiet_seconds,"nextInputId":next_input_id,"scopeRevision":scope.revision,"controlRevision":state.revision,
            "providerHistory":{"digest":history.digest,"messages":len(history.messages),"pages":history.pages,
                "reachedStart":history.reached_start,"oldest":_iso(oldest),"startedAt":_iso(history.started_at),
                "endedAt":_iso(history.ended_at),"creatorId":creator},**evidence}
        row.status,row.reason,row.updated_at="cancelled",RELEASED_UNCONFIRMED_SEND,now
        # Retain the reservation and every wire field. Release only this hold.
        authority._release_hold(s,row,tenant)
        from conversation_engine.delivery import released_unconfirmed
        released_unconfirmed(s,row)
        _attach_evidence(s,row,"release",{"command_key":command_key,"released_at":_iso(now),"history_digest":history.digest,
            "history_messages":len(history.messages),"quiet_seconds":quiet_seconds,"next_input_id":next_input_id,
            "outcome":"unconfirmed"})
        return _record(s,runtime,command_key,"release_unconfirmed_send",request_hash,actor,reason,before,
            {"released":True,"deliveryOutcome":"unconfirmed","intentId":intent_id,"attemptId":row.attempt_id,
             "nextInputId":next_input_id,"historyDigest":history.digest},now)
