"""Durable message dispatch with explicitly scoped registered transports.

The account lock orders policy, control and input checks before a single
dispatching transition commits. Neither a lease nor a restart can grant a
second transport invocation. Ambiguous outcomes retain their reservation and
hold; accepted evidence is saved before repairable transcript projection.

No provider client, credentials, model invocation or command route is constructed here.
Native credentials and account activation are supplied explicitly by deployment.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import Chat, Fan, Message, OfConversationState, OfCreatorBinding, OfMessageDelivery
from db.onlyfans_models import OfIngress, OfRuntime, OfWork
from db.onlyfans_delivery_models import ENGINE_TRANSPORT_KINDS, OfDeliveryPolicy, OfDeliveryPolicyChange, OfTextIntent, OfNativePlan
from onlyfans_read.contract import UNRESOLVED_OUTCOMES
from .store import Claim, Refused, Store, _id, utcnow

MAX_QUEUED = 1000
MAX_SYNC_AGE_SECONDS = 1800
DISPATCH_SECONDS = 30


@dataclass(frozen=True)
class Dispatch:
    account_id: str
    fan_id: int
    intent_id: str
    attempt_id: str
    token: str
    body: str
    payload_hash: str
    origin: str
    deadline: datetime
    policy_revision: int = 0
    wire_version: int = 0
    media_ids: tuple[int, ...] = ()
    preview_ids: tuple[int, ...] = ()
    price_cents: int = 0


@dataclass(frozen=True)
class Accepted:
    account_id: str
    fan_id: int
    intent_id: str
    attempt_id: str
    payload_hash: str
    message_id: int
    occurred_at: str
    evidence_ref: str


@dataclass(frozen=True)
class NotAccepted:
    account_id: str
    fan_id: int
    intent_id: str
    attempt_id: str
    payload_hash: str
    occurred_at: str
    evidence_ref: str


class ProviderThrottled(Exception):
    """A definite 429 response; retry_after is the provider's value when supplied."""

    def __init__(self, retry_after=None):
        super().__init__("provider-rate-limited")
        self.retry_after = retry_after


class SyntheticTextTransport:
    """Explicit local failure-injection seam; it has no network implementation."""

    def __init__(self, account_id, *, message_id=9001, outcome="accepted", on_send=None):
        if outcome not in {"accepted", "not_accepted", "timeout", "malformed", "accepted_lost_response"}:
            raise Refused("invalid synthetic outcome")
        self.account_id = account_id
        self.message_id = message_id
        self.outcome = outcome
        self.on_send = on_send
        self.calls: list[Dispatch] = []

    def send(self, dispatch: Dispatch, *, authority, tenant_id):
        if type(authority) is not TextAuthority or type(dispatch) is not Dispatch:
            raise Refused("invalid synthetic dispatch context")
        if dispatch.account_id != self.account_id:
            raise Refused("synthetic transport scope mismatch")
        dispatch = authority.consume_dispatch(tenant_id, dispatch)
        if dispatch is None:
            raise Refused("dispatch is no longer eligible")
        self.calls.append(dispatch)
        if self.on_send is not None:
            self.on_send(dispatch)
        if self.outcome in {"timeout", "accepted_lost_response"}:
            raise TimeoutError("synthetic ambiguous transport")
        if self.outcome == "malformed":
            return None
        proof = dict(account_id=dispatch.account_id, fan_id=dispatch.fan_id,
                     intent_id=dispatch.intent_id, attempt_id=dispatch.attempt_id,
                     payload_hash=dispatch.payload_hash,
                     occurred_at=datetime.now(timezone.utc).isoformat(),
                     evidence_ref="synthetic:" + dispatch.attempt_id)
        if self.outcome == "not_accepted":
            return NotAccepted(**proof)
        return Accepted(**proof, message_id=self.message_id)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _stamp(raw):
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise Refused("invalid receipt timestamp") from exc
    if value.tzinfo is None:
        raise Refused("receipt timestamp requires timezone")
    value = value.astimezone(timezone.utc).replace(tzinfo=None)
    if value > utcnow() + timedelta(minutes=5):
        raise Refused("receipt timestamp is in the future")
    return value


def _fan_isolated(policy):
    """Engine (fansapi) accounts admit one fan at a time, never one account at a time.

    A send that is in flight or whose outcome is unknown holds ONLY its own fan: the per-fan
    ``OfConversationState.unresolved_attempt_id`` (and the engine hold reasons built on it) keeps that
    fan's dependents from sending, and the account pointer ``active_intent_id`` is not used. Other fans
    proceed. The account pacing gap (``next_dispatch_at``) is unchanged. Legacy gap-mode accounts keep
    the single account-wide slot exactly as deployed.
    """
    return policy is not None and policy.transport_kind == "fansapi"


class TextAuthority:
    """Internal, scoped intent service sharing the existing Store connection pool."""

    def __init__(self, store: Store):
        self.store = store
        self.engine = store.engine

    def _engagement(self):
        # Hooks share this authority's clock and the caller's open transaction.
        from .launch_engagement.service import EngagementService
        service = EngagementService(self.store)
        service.authority = self
        return service

    @staticmethod
    def _db_now(s):
        return s.scalar(select(func.clock_timestamp())).astimezone(timezone.utc).replace(tzinfo=None)

    @staticmethod
    def _policy_values(policy):
        values = {key: getattr(policy, key) for key in
                  ("revision", "enabled", "media_enabled", "transport_kind", "limit_units", "used_units", "queued_count", "throttle_seconds", "active_intent_id")}
        values["next_dispatch_at"] = policy.next_dispatch_at.isoformat() if policy.next_dispatch_at else None
        return values

    @staticmethod
    def _wire_hash(account_id, fan_id, body, media_ids=(), preview_ids=(), price_cents=0):
        wire = dict(account_id=account_id, fan_id=fan_id, text=body, media_ids=list(media_ids), price_cents=price_cents)
        # Preserve every historical text-only hash exactly.
        if media_ids:
            wire["preview_ids"] = list(preview_ids)
        return hashlib.sha256(_json(wire).encode()).hexdigest()

    def _intent_hash(self, row):
        try:
            return self._wire_hash(row.account_id, row.fan_id, row.body,
                json.loads(row.media_json), json.loads(row.preview_json), row.price_cents)
        except (ValueError, TypeError):
            return None

    def _input_current(self, s, row, state):
        if row.input_revision == state.input_revision:
            return True
        delta=state.input_revision-row.input_revision
        if row.origin!="manual" or not 0<delta<=MAX_QUEUED:
            return False
        # Only this operator's already confirmed outputs may advance a queued
        # manual message's transcript. New fan input and unrelated edits still
        # invalidate it; controls and media terms are checked independently.
        changed=s.execute(select(Message,OfTextIntent).outerjoin(OfTextIntent,
            (OfTextIntent.account_id==Message.account_id)&(OfTextIntent.fan_id==Message.fan_id)
            &(OfTextIntent.provider_message_id==Message.message_id)&(OfTextIntent.origin=="manual")
            &(OfTextIntent.actor==row.actor)&(OfTextIntent.control_revision==row.control_revision)
            &(OfTextIntent.status=="accepted")&(OfTextIntent.projected_at>=row.created_at))
            .where(Message.account_id==row.account_id,Message.fan_id==row.fan_id,
                Message.ingested_at>=row.created_at).limit(delta+1)).all()
        return len(changed)==delta and all(proof is not None and msg.direction=="out" and not msg.is_unsent
            and msg.body==proof.body and msg.price_cents==proof.price_cents and msg.media_ids==proof.media_json
            and proof.payload_hash==self._intent_hash(proof) for msg,proof in changed)

    @staticmethod
    def _media_current(s, row, policy, now):
        from conversation_engine.delivery import media_current as engine_media_current
        runtime = s.get(OfRuntime, row.account_id)
        # Apply the shared media switch before any engine-specific shortcut.
        if row.catalog_item_id is not None and (policy is None or not policy.media_enabled):
            return False
        engine_result = engine_media_current(s, runtime, row)
        if engine_result is not None:
            return engine_result
        if row.origin == "manual" and row.catalog_snapshot_json and json.loads(row.catalog_snapshot_json).get("manual_vault") is True:
            from .manual_media import matches
            runtime = s.get(OfRuntime, row.account_id)
            if policy is None or not policy.media_enabled or not matches(s, runtime, row):
                return False
            if row.price_cents > 0:
                from .media_delivery import assert_unowned
                try:
                    assert_unowned(s, row.account_id, row.fan_id, json.loads(row.catalog_snapshot_json), manual=True)
                except (Refused, ValueError, TypeError):
                    return False
            return True
        if row.moxy_event_id is not None:
            from moxy_replacement.delivery import media_current
            return media_current(s, row)
        if row.catalog_item_id is None:
            return row.media_json == "[]" and row.preview_json == "[]" and row.price_cents == 0 and row.catalog_snapshot_json is None
        if policy is None or not policy.media_enabled:
            return False
        from .media_delivery import terms_match, assert_unowned
        if not terms_match(s, row):
            return False
        try:
            assert_unowned(s, row.account_id, row.fan_id, json.loads(row.catalog_snapshot_json))
        except (Refused, ValueError, TypeError):
            return False
        if row.origin == "automated":
            from .native_sales import matches
            return row.native_plan_id is not None and matches(s, row, now)
        return True

    def configure(self, tenant_id, account_id, *, expected_revision, enabled,
                  limit_units, throttle_seconds, actor, reason, media_enabled=None,
                  transport_kind=None):
        with Session(self.engine) as s, s.begin():
            runtime = self.store._runtime(s, tenant_id, account_id)
            return self._configure_in_session(s, runtime, expected_revision=expected_revision,
                enabled=enabled, limit_units=limit_units, throttle_seconds=throttle_seconds,
                actor=actor, reason=reason, media_enabled=media_enabled, transport_kind=transport_kind)

    def _configure_in_session(self, s, runtime, *, expected_revision, enabled, limit_units,
                              throttle_seconds, actor, reason, media_enabled=None,
                              transport_kind=None, daily_limit_units=None):
        """Shared policy owner; caller holds the runtime lock and transaction."""
        account_id = runtime.account_id
        if (daily_limit_units is not None and (type(daily_limit_units) is not int or not 1 <= daily_limit_units <= 20000)):
            raise Refused("invalid delivery policy")
        if (type(expected_revision) is not int or expected_revision < 0 or type(enabled) is not bool
                or type(limit_units) is not int or not 0 <= limit_units <= 10000
                or type(throttle_seconds) is not int or not 1 <= throttle_seconds <= 86400
                or not isinstance(actor, str) or not 1 <= len(actor) <= 128
                or not isinstance(reason, str) or not 1 <= len(reason) <= 512
                or media_enabled is not None and type(media_enabled) is not bool
                or transport_kind is not None and transport_kind not in {"synthetic", "native", "fansapi"}):
            raise Refused("invalid delivery policy")
        from conversation_engine.preparation import assert_send_allowed
        if enabled or media_enabled is True:
            assert_send_allowed(s, runtime)
        from conversation_engine.pilot_scope import current as current_pilot, authorize
        pilot, config = current_pilot(s, runtime)
        if pilot is not None:
            safe_disable = actor == 'fans-webhook' and not enabled
            if not safe_disable:
                authorize(config, actor, 'activate' if enabled else 'stop')
            if (limit_units > config['limits']['send_units'] or throttle_seconds < config['limits']['throttle_seconds']
                    or transport_kind is not None and transport_kind != 'fansapi'):
                raise Refused('delivery policy exceeds the durable pilot scope')
            if config.get('delivery_mode') == 'text_only' and enabled and media_enabled is True:
                raise Refused('text-only pilot cannot enable media')
            if daily_limit_units is not None and daily_limit_units > config['limits']['daily_send_units']:
                raise Refused('daily limit exceeds the durable pilot scope')
        policy = s.scalar(select(OfDeliveryPolicy).where(
            OfDeliveryPolicy.account_id == account_id).with_for_update())
        if policy is None:
            if expected_revision != 0:
                raise Refused("policy revision conflict")
            policy = OfDeliveryPolicy(account_id=account_id)
            s.add(policy)
            s.flush()
        if policy.revision != expected_revision:
            raise Refused("policy revision conflict")
        if pilot is not None and config.get('delivery_mode') == 'text_only':
            effective_media = policy.media_enabled if media_enabled is None else media_enabled
            unchanged_safety_stop = safe_disable and not enabled and effective_media is policy.media_enabled
            if effective_media and not unchanged_safety_stop:
                raise Refused('text-only pilot cannot enable media')
        if pilot is not None and safe_disable and (limit_units != policy.limit_units or
                throttle_seconds != policy.throttle_seconds or media_enabled not in (None, policy.media_enabled) or
                transport_kind not in (None, policy.transport_kind)):
            raise Refused('provider safety stop cannot change pilot limits')
        if transport_kind is not None and transport_kind != policy.transport_kind:
            if (policy.active_intent_id or policy.queued_count or s.scalar(select(OfTextIntent.intent_id).where(
                    OfTextIntent.account_id == account_id, OfTextIntent.status.in_(("dispatching", "unknown"))).limit(1))):
                raise Refused("delivery mode cannot change with active or queued work")
            if {transport_kind, policy.transport_kind} == {"fansapi", "native"}:
                # The legacy Moxy sender and the engine's real provider never swap directly.
                raise Refused("delivery mode cannot switch between native and fansapi")
        before = self._policy_values(policy)
        if daily_limit_units is not None:
            before["daily_limit_units"] = policy.daily_limit_units
        if throttle_seconds > policy.throttle_seconds and policy.next_dispatch_at is not None:
            policy.next_dispatch_at = max(policy.next_dispatch_at,
                self._db_now(s) + timedelta(seconds=throttle_seconds))
        policy.revision += 1
        policy.enabled, policy.limit_units, policy.throttle_seconds = enabled, limit_units, throttle_seconds
        if daily_limit_units is not None:
            policy.daily_limit_units = daily_limit_units
        if media_enabled is not None:
            policy.media_enabled = media_enabled
        if transport_kind is not None:
            policy.transport_kind = transport_kind
        # Policy changes never reset used units, holds, throttle or queue.
        after = self._policy_values(policy)
        if daily_limit_units is not None:
            after["daily_limit_units"] = policy.daily_limit_units
        s.add(OfDeliveryPolicyChange(account_id=account_id, revision=policy.revision,
                                     actor=actor, reason=reason, before_json=_json(before),
                                     after_json=_json(after), created_at=utcnow()))
        return policy.revision


    @staticmethod
    def _action(row):
        from .conversation_policy import action_of
        if row.moxy_event_id:
            from moxy_replacement.delivery import action_class
            return action_class(row.catalog_snapshot_json)
        return action_of(row.origin, row.catalog_item_id)

    def _safety_scan(self, tenant_id, account_id, fan_id, origin):
        """Run the shared inbound detectors for an automated / campaign / engagement send. Never raises into the send:
        a scan failure leaves the existing holds in force and the policy check that follows is still authoritative."""
        if origin == "manual":
            return
        from . import operator_cases
        try:
            operator_cases.scan(self.store, tenant_id, account_id, fan_id)
        except Exception:                      # noqa: BLE001 - detection must never become a send path
            import logging
            logging.getLogger("onlyfans.safety").exception("safety scan failed")

    def _safety_scan_intent(self, tenant_id, account_id, intent_id, *, status="queued"):
        with Session(self.engine) as s:
            row = s.get(OfTextIntent, (account_id, intent_id))
            from conversation_engine.delivery import is_engine_intent
            target = ((row.fan_id, row.origin) if row is not None and row.status == status
                      and not is_engine_intent(s, row) else None)
        if target is not None:
            self._safety_scan(tenant_id, account_id, *target)

    @staticmethod
    def _retained_hold(s, account_id, fan_id, *, own_attempt_id=None):
        from db.onlyfans_engine_models import OfEngineIndependentAdmission
        independent = select(OfEngineIndependentAdmission.attempt_id).where(
            OfEngineIndependentAdmission.account_id == account_id, OfEngineIndependentAdmission.fan_id == fan_id,
            OfEngineIndependentAdmission.status.in_(("dispatching", "unknown")))
        if own_attempt_id is not None:
            independent = independent.where(OfEngineIndependentAdmission.attempt_id != own_attempt_id)
        return (Store._native_unresolved(s, account_id, fan_id) or s.scalar(independent.limit(1)) is not None
            or s.scalar(select(OfMessageDelivery.message_id).where(
            OfMessageDelivery.account_id == account_id, OfMessageDelivery.fan_id == fan_id,
            OfMessageDelivery.outcome.in_(UNRESOLVED_OUTCOMES)).limit(1)) is not None)

    @staticmethod
    def _manual_attachment(row):
        """Derive the narrow archive exception from the saved immutable intent."""
        if (row.origin != "manual" or type(row.price_cents) is not int
                or not (row.price_cents == 0 or 300 <= row.price_cents <= 20000)
                or row.catalog_item_id is None):
            return False
        try:
            snapshot = json.loads(row.catalog_snapshot_json)
            return (snapshot.get("manual_vault") is True
                    and type(snapshot.get("price_cents")) is int
                    and snapshot["price_cents"] == row.price_cents
                    and bool(snapshot.get("media_ids")))
        except (ValueError, TypeError, AttributeError):
            return False

    @staticmethod
    def _verified_single_fan_pilot(s, runtime, fan, origin):
        """Use verified recipient history for the explicitly scoped Moxy pilot."""
        if origin != "automated" or fan is None:
            return False
        from db.onlyfans_moxy_models import MoxyAccount
        from .store import fan_history_current

        owner = s.get(MoxyAccount, runtime.account_id)
        if owner is None or owner.mode != "on":
            return False
        try:
            recipients = json.loads(owner.settings_json)["pilot"]["fan_ids"]
        except (ValueError, TypeError, KeyError):
            return False
        return (type(recipients) is list and len(recipients) == 1
                and type(recipients[0]) is int and recipients[0] == fan.fan_id
                and fan_history_current(s, runtime.account_id, fan.fan_id))

    def _eligibility(self, s, runtime, policy, state, fan, origin, now, *, own_attempt_id=None,
                     manual_attachment=False, action=None, engine_action=False, independent=False):
        if policy is None or not policy.enabled or policy.transport_kind not in {"synthetic", "native", "fansapi"}:
            return "delivery_disabled"
        if not runtime.enabled:
            return "account_disabled"
        if action == 'commercial' and not policy.media_enabled:
            return "media_delivery_disabled"
        # Explicitly selected attachments are authored and priced by the operator.
        # Purchase/ownership checks still run for paid media at every boundary.
        # Text-only messages and automated origins retain their archive gates.
        needs_history = not (origin == "manual" and manual_attachment)
        if needs_history and (runtime.recovery_needed or runtime.pending_events or runtime.last_sync_at is None
                or runtime.last_sync_at < now - timedelta(seconds=MAX_SYNC_AGE_SECONDS)
                or runtime.last_sync_at > now + timedelta(minutes=5)):
            from .store import fan_history_current
            from .fastt_manual import fastt_owned
            # A human message on a fan the FASTT runner owns is not composed from a
            # transcript, so the account-wide sync gate does not apply to it.
            human_on_fastt_fan = origin == "manual" and fan is not None and fastt_owned(runtime.account_id, fan.fan_id)
            if not (fan is not None and fan_history_current(s, runtime.account_id, fan.fan_id)) and not human_on_fastt_fan:
                return "history_not_current"
        if needs_history:
            quarantined = select(OfIngress.event_key).where(
                OfIngress.account_id == runtime.account_id, OfIngress.status == "quarantined")
            if s.scalar(quarantined.limit(1)) is not None:
                # Engine isolation uses verified fan/binding identity, never Moxy
                # ownership. Unknown recipients/bindings remain account-wide holds.
                if not engine_action and not self._verified_single_fan_pilot(s, runtime, fan, origin):
                    return "history_quarantined"
                if fan is None:
                    return "history_quarantined"
                recipient_conflict = quarantined.where(
                    OfIngress.fan_id.is_(None) | (OfIngress.fan_id == fan.fan_id))
                if engine_action:
                    known_fan = select(Fan.fan_id).where(Fan.account_id == runtime.account_id)
                    recipient_conflict = quarantined.where(
                        OfIngress.fan_id.is_(None) | (OfIngress.fan_id == fan.fan_id)
                        | OfIngress.binding_id.is_(None) | (OfIngress.binding_id != runtime.binding_id)
                        | ~OfIngress.fan_id.in_(known_fan))
                if s.scalar(recipient_conflict.limit(1)) is not None:
                    return "history_quarantined"
        if engine_action:
            from conversation_engine.controls import reason as engine_hold_reason
            unresolved = ((state is not None and state.unresolved_attempt_id is not None
                           and state.unresolved_attempt_id != own_attempt_id)
                          or (fan is not None and self._retained_hold(s, runtime.account_id, fan.fan_id, own_attempt_id=own_attempt_id)))
            return engine_hold_reason(s, runtime.account_id, fan.fan_id if fan is not None else 0,
                                      state, fan, unresolved=unresolved and not independent, commercial=action == 'commercial')
        # The conversation-hold policy is ONE pure function shared with the FASTT runner plane (conversation_policy).
        # Everything below this line is a caller of it: this method only reads the durable facts.
        from . import conversation_policy as policy_module, operator_cases
        if state is None or fan is None:
            return "conversation_unavailable"
        skip, critical, cases = operator_cases.policy_facts(s, runtime.account_id, fan.fan_id)
        unresolved = ((state.unresolved_attempt_id is not None and state.unresolved_attempt_id != own_attempt_id)
                      or self._retained_hold(s, runtime.account_id, fan.fan_id, own_attempt_id=own_attempt_id))
        decision = policy_module.decide(
            policy_module.Facts(mode=state.mode, takeover_state=state.takeover_state, muted=bool(fan.is_muted),
                                skip_reason=skip, unresolved=unresolved, critical_flag=critical, cases=cases),
            origin, action or policy_module.action_of(origin))
        return decision.reason

    @staticmethod
    def _valid_work(work, row, state, now):
        return (work is not None and row is not None and state is not None and row.status == "claimed"
                and row.generation == work.generation and row.lease_token == work.token
                and row.control_revision == work.control_revision == state.revision
                and work.input_revision == state.input_revision
                and row.target_message_id == work.target_message_id
                and row.lease_until is not None and row.lease_until > now)

    def prepare(self, tenant_id, account_id, fan_id, *, command_key, expected_revision,
                body, origin, actor, work: Claim | None = None, ttl_seconds=300, native_plan_id=None,
                catalog_item_id=None, quoted_price_cents=None, campaign_id=None, engagement_action_id=None, manual_media=None, moxy_event_id=None, engine_decision_id=None):
        from onlyfans_read.agency_scope import current_scope, actor_allowed
        if current_scope.get() is not None and (origin != "manual" or not actor_allowed(actor, capability="chat")):
            raise Refused("manual messaging permission required")
        _id(fan_id)
        if engine_decision_id is not None:
            if (type(engine_decision_id) is not str or len(engine_decision_id) != 36 or origin != 'automated'
                    or moxy_event_id is not None or work is not None or native_plan_id is not None
                    or manual_media is not None):
                raise Refused('invalid_engine_decision_reference')
        elif moxy_event_id is None:
            from moxy_replacement.routing import route_legacy
            route_legacy(self, tenant_id, account_id, fan_id, origin, command_key,
                {'campaign_id': campaign_id, 'engagement_action_id': engagement_action_id})
        elif type(moxy_event_id) is not str or len(moxy_event_id) != 36 or origin != 'automated' or work is not None or native_plan_id is not None:
            raise Refused('invalid_moxy_decision_reference')
        if (not isinstance(command_key, str) or not 1 <= len(command_key) <= 64
                or type(expected_revision) is not int or expected_revision < 0
                or not isinstance(body, str) or (not body.strip() and manual_media is None) or len(body.encode()) > 20000
                or origin not in {"automated", "manual", "campaign", "engagement"}
                or not isinstance(actor, str) or not 1 <= len(actor) <= 128
                or type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 600):
            raise Refused("invalid text intent")
        if origin in {"automated", "engagement"}:
            from native_pilot.media_claims import echoes_instruction
            if echoes_instruction(body):
                raise Refused("instruction text held")
        if (origin == "campaign" and (campaign_id is None or work is not None or native_plan_id is not None)
                or origin != "campaign" and campaign_id is not None):
            raise Refused("invalid campaign intent")
        if (origin == "engagement") != (engagement_action_id is not None) or (
                origin == "engagement" and (type(engagement_action_id) is not str or len(engagement_action_id) != 36
                    or work is not None or native_plan_id is not None or campaign_id is not None
                    or quoted_price_cents is not None)):
            raise Refused("invalid engagement intent")
        if work is not None and (not isinstance(work, Claim) or work.account_id != account_id or work.fan_id != fan_id):
            raise Refused("work scope mismatch")
        if catalog_item_id is not None:
            _id(catalog_item_id)
            if origin == "automated" and native_plan_id is None and moxy_event_id is None and engine_decision_id is None:
                raise Refused("automated media requires its native plan")
        if quoted_price_cents is not None and (catalog_item_id is None or (native_plan_id is None and moxy_event_id is None and engine_decision_id is None)):
            raise Refused("only a recorded native quote may change the sticker price")
        if manual_media is not None:
            from .manual_media import normalize
            if origin != "manual" or catalog_item_id is not None or native_plan_id is not None:
                raise Refused("invalid manual media selection")
            manual_media = normalize(manual_media)
        request = dict(account_id=account_id, fan_id=fan_id, command_key=command_key,
                       expected_revision=expected_revision, body=body, origin=origin,
                       actor=actor, work=asdict(work) if work else None, ttl_seconds=ttl_seconds)
        if moxy_event_id is not None:
            request['moxy_event_id'] = moxy_event_id
        if engine_decision_id is not None:
            request['engine_decision_id'] = engine_decision_id
        if campaign_id is not None:
            request["campaign_id"] = campaign_id
        if engagement_action_id is not None:
            request["engagement_action_id"] = engagement_action_id
        if native_plan_id is not None:
            if type(native_plan_id) is not str or len(native_plan_id) != 36 or origin != "automated" or work is None:
                raise Refused("invalid native plan reference")
            request["native_plan_id"] = native_plan_id
        if catalog_item_id is not None:
            request["catalog_item_id"] = catalog_item_id
            request["quoted_price_cents"] = quoted_price_cents
        if manual_media is not None:
            request["manual_media"] = manual_media
        fingerprint = hashlib.sha256(_json(request).encode()).hexdigest()
        # Plan-time safety scan (R1): a stop / distress / consent / complaint message is recorded (shadow) or enforced
        # (live) BEFORE this transaction takes its locks, so the eligibility check below sees the hold.
        if engine_decision_id is None:
            self._safety_scan(tenant_id, account_id, fan_id, origin)
        with Session(self.engine) as s, s.begin():
            runtime = self.store._delivery_runtime(s, tenant_id, account_id)
            now = self._db_now(s)
            prior = s.scalar(select(OfTextIntent).where(OfTextIntent.account_id == account_id,
                                                       OfTextIntent.command_key == command_key))
            if prior is not None:
                if prior.command_hash != fingerprint:
                    raise Refused("idempotency key conflict")
                return prior.intent_id
            # Queued-count admission and policy checks are account-wide. Lock the
            # policy row while preparing so parallel manual/campaign callers cannot
            # lose a queued_count increment or admit past MAX_QUEUED.
            policy = s.scalar(select(OfDeliveryPolicy).where(
                OfDeliveryPolicy.account_id == account_id).with_for_update())
            state = s.get(OfConversationState, (account_id, fan_id))
            fan = s.get(Fan, (account_id, fan_id))
            from conversation_engine.delivery import prepare as prepare_engine_decision
            engine_decision = prepare_engine_decision(s, runtime, fan_id, origin, engine_decision_id, body,
                                                      catalog_item_id, quoted_price_cents)
            if origin == "manual" and policy is not None and policy.transport_kind == "fansapi":
                from conversation_engine.pilot_scope import current as current_pilot
                pilot, _ = current_pilot(s, runtime)
                if pilot is not None:
                    # The Fans API transport requires an engine decision. Refuse
                    # unsupported manual work before it can reserve an attempt
                    # and become a false unknown without ever reaching the wire.
                    raise Refused('pilot manual transport is not qualified')
            if engine_decision is None and origin != "manual" and policy is not None and policy.transport_kind == "fansapi":
                # A fansapi account has exactly one automated writer: the conversation engine. A campaign,
                # engagement or legacy automated intent for any fan of it is a competing writer, even before the
                # fan has an owner row. Only a human (origin manual, takeover checked below) may write besides it.
                raise Refused("conversation owned by the new engine")
            from conversation_engine.independence import certified_anchor
            independent_anchor = certified_anchor(s, runtime, engine_decision, state, policy) if engine_decision is not None else None
            if engine_decision is not None and (moxy_event_id is not None or native_plan_id is not None or work is not None):
                raise Refused('engine action cannot carry a legacy decision')
            legacy_action = None
            if engine_decision is None:
                from .conversation_policy import action_of
                legacy_action = action_of(origin, catalog_item_id)
            moxy_event = None
            moxy_terms = None
            moxy_action = None
            if moxy_event_id is not None:
                from moxy_replacement.delivery import prepare_decision
                moxy_event, moxy_terms, moxy_action = prepare_decision(s, account_id, fan_id, moxy_event_id, body)
            reason = self._eligibility(s, runtime, policy, state, fan, origin, now,
                manual_attachment=manual_media is not None,
                action=('commercial' if catalog_item_id is not None else 'chat') if engine_decision is not None else moxy_action or legacy_action,
                engine_action=engine_decision is not None, independent=independent_anchor is not None)
            if reason:
                raise Refused(reason)
            from .handover import staff_owns_takeover
            if origin == "manual" and not staff_owns_takeover(s, account_id, fan_id, state, actor):
                raise Refused("takeover belongs to another team member")
            if state.revision != expected_revision:
                raise Refused("control revision conflict")
            if policy.queued_count >= MAX_QUEUED:
                raise Refused("delivery queue full")
            from . import concurrent_admission
            if policy.active_intent_id and not concurrent_admission.mode(policy) == "concurrent" and independent_anchor is None:
                raise Refused("account delivery unresolved")
            recipient = None
            if origin == "campaign":
                from .campaigns import validate_prepare
                recipient = validate_prepare(s, runtime, fan_id=fan_id, body=body, campaign_id=campaign_id,
                    command_key=command_key, actor=actor, now=now, catalog_item_id=catalog_item_id)
            engagement = None
            if origin == "engagement":
                # Same transaction as the intent below: validation and attach commit together.
                engagement = self._engagement()
            terms = None
            if manual_media is not None:
                if not policy.media_enabled:
                    raise Refused("media delivery disabled")
                from .manual_media import freeze
                catalog_item_id, terms = freeze(s, runtime, manual_media)
                if terms["price_cents"] > 0:
                    from .media_delivery import assert_unowned
                    assert_unowned(s, account_id, fan_id, terms, manual=True)
            elif moxy_event is not None and moxy_terms is not None:
                if not policy.media_enabled or moxy_terms['item']['id'] != catalog_item_id or moxy_terms['price_cents'] != quoted_price_cents:
                    raise Refused('moxy_media_terms_changed')
                terms = moxy_terms
            elif engine_decision is not None and engine_decision.action == 'offer':
                from conversation_engine.commerce import verified_terms
                from db.onlyfans_engine_models import OfEngineOfferGrant, OfEngineOwner
                grant = s.get(OfEngineOfferGrant, (account_id, engine_decision.offer_grant_id))
                terms = verified_terms(s, runtime, s.get(OfEngineOwner, (account_id, fan_id)), grant)
                if terms['item']['id'] != catalog_item_id or terms['price_cents'] != quoted_price_cents:
                    raise Refused('engine offer terms changed')
            elif catalog_item_id is not None:
                from .media_delivery import catalog_terms, assert_unowned
                if not policy.media_enabled:
                    raise Refused("media delivery disabled")
                terms = catalog_terms(s, account_id, catalog_item_id, price_cents=quoted_price_cents)
                assert_unowned(s, account_id, fan_id, terms)
                if origin != "manual":
                    # R6/F09: one payment obligation is requested once, whichever send path asks.
                    from .quote import request_exists
                    if request_exists(s, account_id, fan_id, terms):
                        raise Refused("payment already requested for this obligation")
                if origin == "automated":
                    from .native_sales import eligible, choice
                    if not eligible(s, account_id, fan_id, now)[0]:
                        raise Refused("native sales currently held")
                    plan = s.get(OfNativePlan, (account_id, native_plan_id))
                    if plan is None or plan.sale_json is None:
                        raise Refused("native sale evidence unavailable")
                    saved = json.loads(plan.sale_json)
                    terms = {**choice(s, account_id, fan_id, catalog_item_id, now),
                        "source_quote": saved["source_quote"], "source_message_id": saved["source_message_id"]}
                    if terms["price_cents"] != quoted_price_cents:
                        raise Refused("native price snapshot changed")
            if origin == "automated" and moxy_event is None and engine_decision is None:
                row = s.get(OfWork, (account_id, fan_id))
                if not self._valid_work(work, row, state, now):
                    raise Refused("stale planning claim")
                if native_plan_id is not None:
                    from .native_context import canonical, versions, cost_matches
                    plan = s.get(OfNativePlan, (account_id, native_plan_id))
                    if (plan is None or plan.status != "ready" or plan.expires_at <= now or not cost_matches(s, plan)
                            or plan.binding_id != runtime.binding_id or plan.fan_id != fan_id
                            or plan.claim_json != canonical(asdict(work)) or plan.body != body
                            or plan.sale_json != (canonical(terms) if terms is not None else None)
                            or plan.context_hash != versions(s, account_id, fan_id)[0]):
                        raise Refused("native plan snapshot changed")
                # Saving this immutable intent consumes the planning claim.
                row.status = "done"
                row.lease_token = row.lease_until = None
            elif work is not None:
                raise Refused("manual intent cannot consume automated work")
            action = None
            if engagement is not None:
                action = engagement.validate_prepare(s, runtime, action_id=engagement_action_id, fan_id=fan_id,
                    command_key=command_key, body=body, terms=terms, now=now)
                if (action.fan_id != fan_id or action.expires_at <= now):
                    raise Refused("engagement_delivery_changed")
            identity = str(uuid.uuid4())
            if native_plan_id is not None:
                plan.status, plan.intent_id = "queued", identity
            intent = OfTextIntent(account_id=account_id, intent_id=identity, binding_id=runtime.binding_id,
                               fan_id=fan_id, command_key=command_key, actor=actor, origin=origin,
                               body=body, command_hash=fingerprint,
                               payload_hash=self._wire_hash(account_id, fan_id, body,
                                   terms["media_ids"] if terms else (), terms["preview_ids"] if terms else (), terms["price_cents"] if terms else 0), control_revision=state.revision,
                               input_revision=state.input_revision, policy_revision=policy.revision,
                               work_generation=work.generation if work else None,
                               work_token=work.token if work else None, status="queued", created_at=now,
                               updated_at=now, expires_at=(min(now + timedelta(seconds=ttl_seconds), plan.expires_at) if native_plan_id is not None
                                   else min(now + timedelta(seconds=ttl_seconds), action.expires_at) if action is not None
                                   else now + timedelta(seconds=ttl_seconds)), wire_version=1,
                               moxy_event_id=moxy_event_id, native_plan_id=native_plan_id, catalog_item_id=catalog_item_id,
                               catalog_snapshot_json=_json(terms) if terms else None,
                               media_json=_json(terms["media_ids"]) if terms else "[]",
                               preview_json=_json(terms["preview_ids"]) if terms else "[]",
                               price_cents=terms["price_cents"] if terms else 0)
            s.add(intent)
            if engine_decision is not None:
                from conversation_engine.delivery import attached as attach_engine_decision
                attach_engine_decision(s, engine_decision, intent)
            if moxy_event is not None:
                moxy_event.status, moxy_event.intent_id = 'queued', identity
                from moxy_replacement.campaigns import attach as attach_moxy_campaign
                attach_moxy_campaign(s,moxy_event,identity)
            if terms and terms["price_cents"] > 0:
                from .quote import from_terms, QUOTED
                intent.quote_json = from_terms(terms, account_id=account_id, fan_id=fan_id).as_json()
                intent.quote_lifecycle = QUOTED
            policy.queued_count += 1
            if recipient is not None:
                from .campaigns import attach
                attach(s, recipient, identity, now)
            if action is not None:
                s.flush()
                engagement.attach(s, runtime, action, intent, now)
            return identity

    @staticmethod
    def _intent(s, account_id, intent_id):
        row = s.get(OfTextIntent, (account_id, intent_id))
        if row is None:
            raise Refused("intent unavailable")
        return row

    def admit(self, tenant_id, account_id, intent_id, *, transport_generation=None):
        """Commit the one dispatch grant. Never reclaim dispatching/unknown."""
        if transport_generation is not None and (type(transport_generation) is not int or transport_generation < 0):
            raise Refused("invalid transport generation")
        self._safety_scan_intent(tenant_id, account_id, intent_id)
        with Session(self.engine) as s, s.begin():
            runtime = self.store._delivery_runtime(s, tenant_id, account_id)
            now = self._db_now(s)
            intent = self._intent(s, account_id, intent_id)
            if intent.status != "queued":
                if intent.status == "dispatching" and intent.wire_version == 0:
                    self._hold_unknown(s, intent, "legacy_dispatch_unverified", tenant_id)
                return intent.status
            policy = s.scalar(select(OfDeliveryPolicy).where(OfDeliveryPolicy.account_id == account_id).with_for_update())
            # Another worker may have admitted/settled this intent while we waited for the policy lock. The
            # intent row loaded above is a pre-lock (stale) read: re-read it under the lock before reserving.
            s.refresh(intent)
            if intent.status != "queued":
                return intent.status
            state = s.get(OfConversationState, (account_id, intent.fan_id))
            fan = s.get(Fan, (account_id, intent.fan_id))
            work = s.get(OfWork, (account_id, intent.fan_id)) if intent.work_generation is not None else None
            obsolete = (intent.wire_version != 1 or intent.expires_at <= now or state is None
                        or intent.binding_id != runtime.binding_id
                        or intent.control_revision != state.revision
                        or not self._input_current(s, intent, state)
                        or intent.payload_hash != self._intent_hash(intent)
                        or not self._media_current(s, intent, policy, now)
                        or policy is None or intent.policy_revision != policy.revision
                        or (transport_generation is not None and transport_generation != intent.policy_revision)
                        or (intent.work_generation is not None and
                            (work is None or work.generation != intent.work_generation)))
            from .handover import staff_owns_takeover
            if not obsolete and intent.origin == "manual":
                obsolete = not staff_owns_takeover(s, account_id, intent.fan_id, state, intent.actor)
            if not obsolete:
                from conversation_engine.delivery import current as engine_current
                owner_valid, engine_action = engine_current(s, runtime, intent)
                obsolete = not owner_valid
                if not obsolete and not engine_action:
                    from moxy_replacement.runtime import current
                    obsolete = not current(s, intent)
            if not obsolete and intent.native_plan_id is not None:
                from .native_context import matches
                obsolete = not matches(s, intent)
            if not obsolete and intent.origin == "campaign":
                from .campaigns import current
                obsolete = not current(s, intent, runtime, now)
            if not obsolete and intent.origin == "engagement":
                obsolete = not self._engagement().current(s, intent, runtime, now)
            from conversation_engine.independence import certified_for_intent
            independent_anchor = certified_for_intent(s, runtime, intent, state, policy) if not obsolete and engine_action else None
            reason = "snapshot_changed" if obsolete else self._eligibility(s, runtime, policy, state, fan, intent.origin, now,
                manual_attachment=self._manual_attachment(intent),
                action=('commercial' if intent.catalog_item_id is not None else 'chat') if engine_action else self._action(intent),
                engine_action=engine_action, independent=independent_anchor is not None)
            if reason is None and engine_action:
                from conversation_engine.pilot_scope import delivery_block
                reason = delivery_block(s, runtime, intent.fan_id, commercial=intent.catalog_item_id is not None)
            if reason == "pilot_evidence_pending" or reason == "history_not_current" and self._waiting_for_ingress(runtime, now):
                # A different fan's pending ingress can temporarily invalidate the
                # account history check. Preserve the current, still-unsent reply;
                # every retry rechecks its snapshot, expiry and conversation holds.
                intent.reason, intent.updated_at = reason, now
                return "queued"
            if reason:
                intent.status = "cancelled" if obsolete else "refused"
                intent.reason, intent.updated_at = reason, now
                from conversation_engine.delivery import unsent_cancelled as engine_unsent_cancelled
                engine_unsent_cancelled(s, intent)
                if policy is not None:
                    policy.queued_count -= 1
                return intent.status
            from . import concurrent_admission
            concurrent = concurrent_admission.mode(policy) == "concurrent"
            isolated = _fan_isolated(policy)
            if (not concurrent and not isolated and independent_anchor is None
                    and (policy.active_intent_id or Store._native_unresolved(s, account_id, dispatching_only=True))):
                intent.reason = "account_busy"
                return "queued"
            if not concurrent and policy.next_dispatch_at is not None and policy.next_dispatch_at > now:
                intent.reason = "account_throttled"
                return "queued"
            # The first part of a multi-part reply reserves against the capacity the WHOLE sequence needs, so a
            # sequence that cannot finish is never started (no first part goes out). Later parts, and every
            # non-engine intent, keep the ordinary one-unit check.
            units_needed = 1
            if engine_action:
                from conversation_engine.delivery import sequence_units_remaining
                units_needed = sequence_units_remaining(s, intent)
            if policy.used_units + units_needed > policy.limit_units:
                exhausted = policy.used_units >= policy.limit_units
                intent.status, intent.updated_at = "refused", now
                intent.reason = "cap_exhausted" if exhausted else "sequence_capacity_insufficient"
                policy.queued_count -= 1
                if engine_action:
                    # Cancel the decision (and the unsent rest of its sequence) so it is not retried every tick.
                    # A distinct reason keeps the autonomous regeneration scan from re-planning against a
                    # capacity that did not change: that would spend a model call per tick for nothing.
                    from conversation_engine.delivery import unsent_cancelled as engine_capacity_cancelled
                    engine_capacity_cancelled(s, intent, reason="delivery_capacity", next_step="review_delivery_capacity")
                return intent.status
            if not concurrent:
                # The rolling daily cap is enforced in gap mode too (concurrent admission checks the same counter): a pilot scope can
                # configure daily_send_units below the lifetime allowance. Hold the intent, never drop it; the window rolls on its own.
                cap = getattr(policy, "daily_limit_units", None)
                if cap is not None and concurrent_admission.daily_used(s, account_id, now) >= int(cap):
                    intent.reason = "account_daily_cap"
                    return "queued"
            token, attempt = str(uuid.uuid4()), str(uuid.uuid4())
            if concurrent:
                slot, reason = concurrent_admission.admit(
                    s, tenant_id, policy, account_id=account_id, fan_id=intent.fan_id,
                    holder=intent.intent_id, attempt_id=attempt, now=now,
                    send_concurrency=concurrent_admission.process_send_concurrency(),
                    dispatch_lease_s=concurrent_admission.process_dispatch_lease_s())
                if reason:
                    intent.reason = reason
                    return "queued"
            policy.used_units += 1
            policy.queued_count -= 1
            if not concurrent:
                if independent_anchor is None and not isolated:
                    policy.active_intent_id = intent.intent_id
                policy.next_dispatch_at = now + timedelta(seconds=policy.throttle_seconds)
            intent.status, intent.reason, intent.updated_at = "dispatching", None, now
            if intent.quote_json:
                from .quote import INTENT
                intent.quote_lifecycle = INTENT
            intent.attempt_id, intent.dispatch_token = attempt, token
            intent.dispatched_at, intent.deadline = now, now + timedelta(seconds=DISPATCH_SECONDS)
            intent.reservation_held = True
            if independent_anchor is None:
                state.unresolved_attempt_id = attempt
            else:
                from conversation_engine.independence import begin_admission
                begin_admission(s, intent, independent_anchor)
            state.updated_at = now
            from conversation_engine.delivery import attempt_started as engine_attempt_started
            engine_attempt_started(s, intent)
            return self._grant(intent)

    @staticmethod
    def _grant(row):
        return Dispatch(row.account_id, row.fan_id, row.intent_id, row.attempt_id, row.dispatch_token,
                        row.body, row.payload_hash, row.origin, row.deadline, row.policy_revision, row.wire_version,
                        tuple(json.loads(row.media_json)), tuple(json.loads(row.preview_json)), row.price_cents)

    @staticmethod
    def _validate_grant(grant):
        if type(grant) is not Dispatch:
            raise Refused("invalid dispatch grant")
        _id(grant.fan_id)
        fields = ((grant.account_id, 128), (grant.intent_id, 36), (grant.attempt_id, 36),
                  (grant.token, 36), (grant.payload_hash, 64))
        if (any(type(value) is not str or not 1 <= len(value.encode()) <= limit for value, limit in fields)
                or len(grant.payload_hash) != 64 or any(c not in "0123456789abcdef" for c in grant.payload_hash)
                or type(grant.origin) is not str or grant.origin not in {"automated", "manual", "campaign", "engagement"}
                or type(grant.deadline) is not datetime or grant.deadline.tzinfo is not None
                or type(grant.policy_revision) is not int or grant.policy_revision < 0
                or type(grant.wire_version) is not int or grant.wire_version not in {0, 1}):
            raise Refused("invalid dispatch grant")
        if (type(grant.body) is not str or len(grant.body.encode()) > 20000
                or (not grant.body.strip() and (grant.origin != "manual" or not grant.media_ids))):
            raise Refused("invalid dispatch grant")
        if (type(grant.media_ids) is not tuple or type(grant.preview_ids) is not tuple
                or len(grant.media_ids) > 35
                or any(type(v) is not int or not 0 < v < 2**63 for v in grant.media_ids)
                or len(set(grant.media_ids)) != len(grant.media_ids)
                or any(type(v) is not int or v not in grant.media_ids for v in grant.preview_ids)
                or len(set(grant.preview_ids)) != len(grant.preview_ids)
                or type(grant.price_cents) is not int
                or (not grant.media_ids and (grant.preview_ids or grant.price_cents != 0))
                or (grant.media_ids and not (300 <= grant.price_cents <= 20000 or grant.price_cents == 0))):
            raise Refused("invalid media dispatch grant")

    def check_dispatch_preflight(self, tenant_id, grant: Dispatch, *, transport_generation):
        """Allow a bounded identity read for a current, tenant-scoped grant.

        This commits no wire marker or new rights. It rejects forged/replayed
        grants before fixture identity I/O; consume_dispatch still owns every
        final control, input, native snapshot, media and allowance check.
        """
        self._validate_grant(grant)
        if type(transport_generation) is not int or transport_generation < 1:
            raise Refused("invalid transport generation")
        with Session(self.engine) as s, s.begin():
            self.store._delivery_runtime(s, tenant_id, grant.account_id)
            row = self._intent(s, grant.account_id, grant.intent_id)
            now = self._db_now(s)
            if (self._grant(row) != grant or row.status != "dispatching" or row.wire_started_at is not None
                    or row.wire_version != 1 or row.deadline <= now or row.expires_at <= now
                    or row.policy_revision != transport_generation):
                raise Refused("dispatch is not eligible for identity preflight")

    def consume_dispatch(self, tenant_id, grant: Dispatch, *, transport_generation=None, retry_after_posts=None):
        """Commit the final one-use network marker, never an I/O transaction.

        ``retry_after_posts`` (2 or more -> never below 1) authorizes ONE further wire attempt of an
        already-consumed grant after the transport saw a provider-proven not-sent result. It never consumes the
        grant again: it re-runs every pre-wire check below (account/fan scope, controls, binding, freshness,
        approval, pilot/budget gates) under the same lock, atomically advances a durable attempt counter so a
        concurrent worker cannot take the same retry, and keeps the immutable key/payload. Any failed check
        refuses WITHOUT changing the intent (the earlier attempt is already proven not sent); the transport then
        settles it as not accepted and makes no further POST.
        """
        self._validate_grant(grant)
        if transport_generation is not None and (type(transport_generation) is not int or transport_generation < 0):
            raise Refused("invalid transport generation")
        retry = retry_after_posts is not None
        if retry and (type(retry_after_posts) is not int or not 1 <= retry_after_posts <= 2):
            raise Refused("invalid retry attempt")
        # Immediately-before-the-wire safety scan (R1): a stop request that landed after planning is judged now.
        self._safety_scan_intent(tenant_id, grant.account_id, grant.intent_id, status="dispatching")
        with Session(self.engine) as s, s.begin():
            runtime = self._historical_runtime(s, tenant_id, grant.account_id)
            row = self._intent(s, grant.account_id, grant.intent_id)
            if self._grant(row) != grant:
                raise Refused("dispatch grant mismatch")
            if retry:
                if row.wire_started_at is None or row.status != "dispatching":
                    raise Refused("no wire attempt is open for a retry")
                expected_marker = None if retry_after_posts == 1 else f"wire_attempt:{retry_after_posts}"
                if row.reason != expected_marker:
                    raise Refused("wire retry already taken")
            elif row.wire_started_at is not None or row.status != "dispatching":
                raise Refused("dispatch grant was already consumed or settled")
            now = self._db_now(s)
            if retry and (row.wire_version != 1 or row.deadline <= now or row.expires_at <= now):
                raise Refused("wire retry window closed")
            if row.wire_version != 1 or row.deadline <= now or row.expires_at <= now:
                # Under the runtime lock and before wire_started_at is written, so nothing was or will be sent
                # for this attempt: a version-1 engine intent is proven never wired. Anything else stays unknown.
                if self._cancel_never_wired(s, row, "prewire_dispatch_expired", tenant_id):
                    return None
                self._hold_unknown(s, row, "legacy_dispatch_unverified" if row.wire_version != 1 else "dispatch_expired",
                                   tenant_id)
                return None
            policy = s.scalar(select(OfDeliveryPolicy).where(OfDeliveryPolicy.account_id == row.account_id).with_for_update())
            state = s.get(OfConversationState, (row.account_id, row.fan_id))
            fan = s.get(Fan, (row.account_id, row.fan_id))
            binding = s.get(OfCreatorBinding, row.binding_id)
            work = s.get(OfWork, (row.account_id, row.fan_id)) if row.work_generation is not None else None
            obsolete = (binding is None or binding.id != runtime.binding_id or binding.revoked_at is not None
                        or state is None or state.revision != row.control_revision
                        or not self._input_current(s, row, state)
                        or policy is None or policy.revision != row.policy_revision
                        or (transport_generation is not None and transport_generation != row.policy_revision)
                        or row.payload_hash != self._intent_hash(row)
                        or not self._media_current(s, row, policy, now)
                        or (row.work_generation is not None and (work is None or work.generation != row.work_generation)))
            from .handover import staff_owns_takeover
            if not obsolete and row.origin == "manual":
                obsolete = not staff_owns_takeover(s, row.account_id, row.fan_id, state, row.actor)
            if not obsolete:
                from conversation_engine.delivery import current as engine_current
                owner_valid, engine_action = engine_current(s, runtime, row)
                obsolete = not owner_valid
                if not obsolete and not engine_action:
                    from moxy_replacement.runtime import current
                    obsolete = not current(s, row)
            if not obsolete and row.native_plan_id is not None:
                from .native_context import matches
                obsolete = not matches(s, row)
            if not obsolete and row.origin == "campaign":
                from .campaigns import current
                obsolete = not current(s, row, runtime, now)
            if not obsolete and row.origin == "engagement":
                obsolete = not self._engagement().current(s, row, runtime, now)
            from conversation_engine.independence import certified_for_intent, admission_for
            independent_anchor = certified_for_intent(s, runtime, row, state, policy) if not obsolete and engine_action else None
            independent_admission = admission_for(s, row)
            reason = "snapshot_changed" if obsolete else self._eligibility(
                s, runtime, policy, state, fan, row.origin, now, own_attempt_id=row.attempt_id,
                manual_attachment=self._manual_attachment(row),
                action=('commercial' if row.catalog_item_id is not None else 'chat') if engine_action else self._action(row),
                engine_action=engine_action, independent=independent_anchor is not None)
            if reason is None and engine_action:
                from conversation_engine.pilot_scope import delivery_block
                reason = delivery_block(s, runtime, row.fan_id, commercial=row.catalog_item_id is not None)
            from . import concurrent_admission
            concurrent = concurrent_admission.mode(policy) == "concurrent"
            if concurrent:
                from native_pilot import admission
                cooldown = admission.cooldown_at(policy)
                if retry and reason is None and concurrent_admission.is_after(cooldown, now):
                    reason = "account_throttled"
                if reason is None and concurrent_admission.is_after(cooldown, now):
                    # A provider-wide cooldown can land after admission but before
                    # the wire. Put this still-unsent intent back in the queue rather
                    # than turning the cooldown into a permanent refusal.
                    row.status, row.reason, row.updated_at = "queued", "account_throttled", now
                    policy.queued_count += 1
                    if row.reservation_held:
                        policy.used_units -= 1
                        row.reservation_held = False
                    self._release_hold(s, row, tenant_id)
                    return None
                if not row.reservation_held or state.unresolved_attempt_id != row.attempt_id \
                        or not concurrent_admission.check_holder(s, tenant_id, row.account_id, row.fan_id,
                                                                  row.intent_id, now):
                    reason = "attempt_ownership_changed"
            elif independent_anchor is not None:
                if (independent_admission is None or independent_admission.status != 'dispatching'
                        or independent_admission.anchor_intent_id != independent_anchor.intent_id or not row.reservation_held):
                    reason = "attempt_ownership_changed"
            elif ((policy.active_intent_id != row.intent_id and not _fan_isolated(policy)) or not row.reservation_held
                  or state.unresolved_attempt_id != row.attempt_id):
                reason = "attempt_ownership_changed"
            if reason is None and policy.used_units > policy.limit_units:
                reason = "cap_exhausted"
            if retry:
                if reason is not None:
                    raise Refused("wire retry no longer authorized: " + reason)
                row.reason, row.updated_at = f"wire_attempt:{retry_after_posts + 1}", now
                return self._grant(row)
            if reason == "pilot_evidence_pending" or reason == "history_not_current" and self._waiting_for_ingress(runtime, now):
                # No wire marker exists yet. Release this attempt's reservation
                # and retain the intent for a fresh admission after ingress drains.
                # Ownership validation above must succeed before this deferral.
                if policy.used_units < 1:
                    raise Refused("delivery reservation unavailable")
                row.status, row.reason, row.updated_at = "queued", reason, now
                policy.queued_count += 1
                policy.used_units -= 1
                row.reservation_held = False
                self._release_hold(s, row, tenant_id)
                return None
            if reason:
                if policy is None or (row.reservation_held and policy.used_units < 1):
                    raise Refused("delivery reservation unavailable")
                row.status = "cancelled" if obsolete else "refused"
                row.reason, row.updated_at = "prewire_" + reason, now
                from conversation_engine.delivery import unsent_cancelled as engine_unsent_cancelled
                engine_unsent_cancelled(s, row)
                if row.reservation_held:
                    policy.used_units -= 1
                    row.reservation_held = False
                self._release_hold(s, row, tenant_id)
                return None
            row.wire_started_at, row.updated_at = now, now
            if not concurrent:
                policy.next_dispatch_at = max(policy.next_dispatch_at or now, now + timedelta(seconds=policy.throttle_seconds))
            return self._grant(row)

    @staticmethod
    def _historical_runtime(s, tenant_id, account_id):
        # A receipt must survive revocation after dispatch. It may only settle
        # the immutable historical binding; it never authorizes another send.
        runtime = s.scalar(select(OfRuntime).where(OfRuntime.account_id == account_id).with_for_update())
        if runtime is None:
            raise Refused("scope unavailable")
        binding = s.scalar(select(OfCreatorBinding).where(
            OfCreatorBinding.id == runtime.binding_id, OfCreatorBinding.account_id == account_id,
            OfCreatorBinding.tenant_id == tenant_id, OfCreatorBinding.channel == "onlyfans").with_for_update(read=True))
        if binding is None:
            raise Refused("scope unavailable")
        return runtime

    def _attempt(self, s, tenant_id, account_id, intent_id, token):
        runtime = self._historical_runtime(s, tenant_id, account_id)
        row = self._intent(s, account_id, intent_id)
        if row.binding_id != runtime.binding_id:
            raise Refused("scope unavailable")
        if not row.dispatch_token or row.dispatch_token != token or not row.attempt_id:
            raise Refused("stale dispatch token")
        return row

    @staticmethod
    def _validate_proof(row, proof):
        _id(proof.fan_id)
        if (proof.account_id, proof.fan_id, proof.intent_id, proof.attempt_id, proof.payload_hash) != (
                row.account_id, row.fan_id, row.intent_id, row.attempt_id, row.payload_hash):
            raise Refused("receipt scope or payload mismatch")
        if not isinstance(proof.evidence_ref, str) or not 1 <= len(proof.evidence_ref) <= 256:
            raise Refused("invalid receipt evidence")
        stamp = _stamp(proof.occurred_at)
        if row.dispatched_at is None or stamp < row.dispatched_at - timedelta(minutes=5):
            raise Refused("receipt predates attempt")
        return stamp

    @staticmethod
    def _hold_unknown(s, row, reason, tenant_id=None):
        row.status, row.reason, row.updated_at = "unknown", reason, utcnow()
        from conversation_engine.delivery import unknown as engine_unknown
        engine_unknown(s, row)
        state = s.get(OfConversationState, (row.account_id, row.fan_id))
        policy = s.get(OfDeliveryPolicy, row.account_id)
        from conversation_engine.independence import retain_unknown
        independent = retain_unknown(s, row)
        if not independent:
            state.unresolved_attempt_id = row.attempt_id
        state.requires_attention, state.updated_at = True, utcnow()
        from . import concurrent_admission
        if not independent and tenant_id is not None and concurrent_admission.mode(policy) == "concurrent":
            gap = max(concurrent_admission.process_conversation_gap_s(),
                      float(getattr(policy, "conversation_min_gap_seconds", 0) or 0))
            concurrent_admission.release(s, tenant_id, row.account_id, row.fan_id, row.intent_id,
                                         row.attempt_id, "unknown", utcnow(), gap)
        elif not independent and not _fan_isolated(policy):
            policy.active_intent_id = row.intent_id

    def _cancel_never_wired(self, s, row, reason, tenant_id=None):
        """Retire a dispatching engine attempt that PROVABLY never reached the wire. Caller holds the runtime lock.

        Invariant relied on: for wire_version 1 every registered transport commits ``wire_started_at`` through
        consume_dispatch (the one-use marker, written under this lock) before it can issue any provider
        request, so a version-1 attempt with no marker has sent nothing. Legacy (wire_version != 1), non-engine
        or ambiguous attempts return False and the caller keeps its unknown hold.

        Atomically: the old dispatch token is destroyed (no late worker can consume, record or mark it), the
        engine decision is cancelled through the same unsent-cancellation lineage as every other pre-wire
        cancel (the request reopens, nothing is replayed), and only this attempt's held delivery-unit
        reservation is released. Model/provider cost is never touched here.
        """
        if row.status != "dispatching" or row.wire_version != 1 or row.wire_started_at is not None:
            return False
        from conversation_engine.delivery import is_engine_intent, unsent_cancelled as engine_unsent_cancelled
        if not is_engine_intent(s, row):
            return False
        policy = s.scalar(select(OfDeliveryPolicy).where(OfDeliveryPolicy.account_id == row.account_id).with_for_update())
        if policy is None or policy.transport_kind not in ENGINE_TRANSPORT_KINDS or (row.reservation_held and policy.used_units < 1):
            return False
        row.status, row.reason, row.updated_at = "cancelled", reason, self._db_now(s)
        row.dispatch_token = None
        engine_unsent_cancelled(s, row)
        if row.reservation_held:
            policy.used_units -= 1
            row.reservation_held = False
        self._release_hold(s, row, tenant_id)
        return True

    def mark_unknown(self, tenant_id, account_id, intent_id, dispatch_token, reason="ambiguous_transport"):
        if reason not in {"ambiguous_transport", "invalid_receipt", "dispatch_expired", "provider_identity_conflict"}:
            raise Refused("invalid uncertainty reason")
        with Session(self.engine) as s, s.begin():
            row = self._attempt(s, tenant_id, account_id, intent_id, dispatch_token)
            if row.status == "dispatching":
                self._hold_unknown(s, row, reason, tenant_id)
            return row.status

    def record_accepted(self, tenant_id, account_id, intent_id, dispatch_token, receipt):
        with Session(self.engine) as s, s.begin():
            row = self._attempt(s, tenant_id, account_id, intent_id, dispatch_token)
            from .send_recovery import RETIRED_UNCONFIRMED_TEXT
            from conversation_engine.delivery import is_engine_intent
            retired = (row.status == "cancelled" and row.reason == RETIRED_UNCONFIRMED_TEXT
                       and not is_engine_intent(s, row))  # engine intents are one-use: never reopened
            if row.status not in {"dispatching", "unknown", "accepted"} and not retired:
                raise Refused("attempt is already settled")
            try:
                if type(receipt) is not Accepted:
                    raise Refused("invalid accepted evidence")
                _id(receipt.message_id)
                self._validate_proof(row, receipt)
                encoded = _json(asdict(receipt))
            except (Refused, TypeError, AttributeError):
                if row.status == "accepted" or retired:
                    raise Refused("accepted receipt conflict")
                self._hold_unknown(s, row, "invalid_receipt", tenant_id)
                return "unknown"
            if row.status == "accepted":
                if row.receipt_json != encoded:
                    raise Refused("accepted receipt conflict")
                return "accepted"
            if row.receipt_json is not None and row.receipt_json != encoded:
                # A valid acceptance can remain unknown because its provider
                # identity conflicts. Uncertainty never makes that proof mutable.
                raise Refused("accepted receipt conflict")
            other = s.scalar(select(OfTextIntent.intent_id).where(
                OfTextIntent.account_id == account_id, OfTextIntent.provider_message_id == receipt.message_id,
                OfTextIntent.intent_id != intent_id).limit(1))
            if other is not None and retired:
                raise Refused("retired attempt provider identity conflict")
            row.receipt_json, row.evidence_ref = encoded, receipt.evidence_ref
            if other is not None:
                self._hold_unknown(s, row, "provider_identity_conflict", tenant_id)
                return "unknown"
            row.status, row.reason, row.updated_at = "accepted", None, utcnow()
            if row.quote_json:
                from .quote import ACCEPTED
                row.quote_lifecycle = ACCEPTED
            row.provider_message_id = receipt.message_id
            from conversation_engine.delivery import attempt_settled as engine_attempt_settled
            engine_attempt_settled(s, row, 'accepted', encoded)
            return "accepted"

    @staticmethod
    def _waiting_for_ingress(runtime, now):
        # Only a fresh account's transient ingress backlog is retryable here.
        # Recovery or stale/missing sync retains its existing refusal behaviour.
        return (bool(runtime.pending_events) and not runtime.recovery_needed
                and runtime.last_sync_at is not None
                and now - timedelta(seconds=MAX_SYNC_AGE_SECONDS) <= runtime.last_sync_at
                <= now + timedelta(minutes=5))

    def _release_hold(self, s, row, tenant_id=None):
        policy = s.get(OfDeliveryPolicy, row.account_id)
        state = s.get(OfConversationState, (row.account_id, row.fan_id))
        from conversation_engine.independence import admission_for, settle_and_promote
        own_admission = admission_for(s, row)
        promote = settle_and_promote(s, row, policy, state)
        from . import concurrent_admission
        if own_admission is None and promote is None and tenant_id is not None and concurrent_admission.mode(policy) == "concurrent":
            gap = max(concurrent_admission.process_conversation_gap_s(),
                      float(getattr(policy, "conversation_min_gap_seconds", 0) or 0))
            concurrent_admission.release(s, tenant_id, row.account_id, row.fan_id, row.intent_id,
                                         row.attempt_id, "accepted", utcnow(), gap)
            state = s.get(OfConversationState, (row.account_id, row.fan_id))
            if state is not None and state.unresolved_attempt_id == row.attempt_id:
                state.unresolved_attempt_id = None
                state.updated_at = utcnow()
            Store.complete_takeover(s, row.account_id, row.fan_id)
            return
        if row.wire_started_at is not None:
            floor = self._db_now(s) + timedelta(seconds=policy.throttle_seconds)
            policy.next_dispatch_at = max(policy.next_dispatch_at or floor, floor)
        if policy.active_intent_id == row.intent_id:
            policy.active_intent_id = None
        state = s.get(OfConversationState, (row.account_id, row.fan_id))
        if state.unresolved_attempt_id == row.attempt_id:
            state.unresolved_attempt_id = None
            state.updated_at = utcnow()
        if promote is not None:
            if not _fan_isolated(policy):
                policy.active_intent_id = promote.intent_id
            state.unresolved_attempt_id = promote.attempt_id
            if promote.status == 'unknown':
                state.requires_attention = True
            state.updated_at = utcnow()
        # Attention may represent another safety concern. Receipt repair never
        # clears that independent flag or resumes replies. Completing a requested
        # human handover is a separate, immutable control event.
        Store.complete_takeover(s, row.account_id, row.fan_id)

    def record_not_accepted(self, tenant_id, account_id, intent_id, dispatch_token, rejection):
        with Session(self.engine) as s, s.begin():
            row = self._attempt(s, tenant_id, account_id, intent_id, dispatch_token)
            if row.status not in {"dispatching", "unknown", "not_accepted"}:
                raise Refused("attempt is already settled")
            if row.receipt_json is not None and "message_id" in json.loads(row.receipt_json):
                raise Refused("accepted evidence must be retained")
            try:
                if type(rejection) is not NotAccepted:
                    raise Refused("invalid rejection evidence")
                self._validate_proof(row, rejection)
                encoded = _json(asdict(rejection))
            except (Refused, TypeError, AttributeError):
                if row.status == "not_accepted":
                    raise Refused("rejection evidence conflict")
                self._hold_unknown(s, row, "invalid_receipt", tenant_id)
                return "unknown"
            if row.status == "not_accepted":
                if row.receipt_json != encoded:
                    raise Refused("rejection evidence conflict")
                return "not_accepted"
            row.status, row.reason, row.updated_at = "not_accepted", "proved_not_accepted", utcnow()
            if row.quote_json:
                from .quote import CANCELLED
                row.quote_lifecycle = CANCELLED
            row.receipt_json, row.evidence_ref = encoded, rejection.evidence_ref
            from conversation_engine.delivery import attempt_settled as engine_attempt_settled, not_accepted as engine_not_accepted
            engine_attempt_settled(s, row, 'not_accepted', encoded)
            engine_not_accepted(s, row)
            if row.reservation_held:
                policy = s.scalar(select(OfDeliveryPolicy).where(
                    OfDeliveryPolicy.account_id == account_id).with_for_update())
                policy.used_units -= 1
                row.reservation_held = False
            self._release_hold(s, row, tenant_id)
            return row.status

    def materialize(self, tenant_id, account_id, intent_id, *, synthetic_engine_only=False):
        """Project a saved receipt atomically. This method cannot call transport."""
        with Session(self.engine) as s, s.begin():
            runtime = self._historical_runtime(s, tenant_id, account_id)
            row = self._intent(s, account_id, intent_id)
            if synthetic_engine_only:
                from conversation_engine.delivery import is_engine_intent
                policy = s.get(OfDeliveryPolicy, account_id)
                if (policy is None or policy.transport_kind not in ENGINE_TRANSPORT_KINDS or row.binding_id != runtime.binding_id
                        or not is_engine_intent(s, row)):
                    raise Refused('synthetic engine recovery scope unavailable')
            if row.status != "accepted" or not row.receipt_json:
                raise Refused("accepted receipt required")
            if row.projected_at is not None:
                return True
            proof = Accepted(**json.loads(row.receipt_json))
            stamp = self._validate_proof(row, proof)
            _id(proof.message_id)
            existing = s.get(Message, (account_id, row.fan_id, proof.message_id))
            wrong_fan = s.scalar(select(Message.fan_id).where(Message.account_id == account_id,
                Message.message_id == proof.message_id, Message.fan_id != row.fan_id).limit(1))
            receipt = s.get(OfMessageDelivery, (account_id, row.fan_id, proof.message_id))
            try:
                existing_media = json.loads(existing.media_ids) if existing is not None else []
            except (ValueError, TypeError):
                existing_media = None
            conflict = (wrong_fan is not None or (existing is not None and
                        (existing.direction != "out" or existing.body != row.body
                         or existing.price_cents != row.price_cents or existing.media_count != len(json.loads(row.media_json))
                         or existing_media != json.loads(row.media_json)))
                        or row.payload_hash != self._intent_hash(row)
                        or (receipt is not None and receipt.attempt_id != row.attempt_id))
            if conflict:
                # Valid acceptance is retained, but conflicting canonical
                # identity blocks the account until evidence is reconciled.
                row.reason, row.updated_at = "canonical_identity_conflict", utcnow()
                state = s.get(OfConversationState, (account_id, row.fan_id))
                state.requires_attention = True
                return False
            state = s.get(OfConversationState, (account_id, row.fan_id))
            if existing is None:
                kind = ("onlyfans_campaign" if row.origin == "campaign" else
                        "onlyfans_engagement" if row.origin == "engagement" else
                        ("onlyfans_media" if row.catalog_item_id is not None else "onlyfans_text") if row.origin == "automated" else None)
                # A human send is stamped with the Twin-staff sentinel when an operator has
                # created it, so FASTT can tell it from the AI; absent, the column stays NULL.
                from .staff_sentinels import human_employee_id
                from conversation_engine.delivery import is_engine_intent
                engine_authored = is_engine_intent(s, row)
                s.add(Message(account_id=account_id, fan_id=row.fan_id, message_id=proof.message_id,
                              direction="out", body=row.body, created_at=stamp, raw_json=row.receipt_json,
                              media_ids=row.media_json, media_count=len(json.loads(row.media_json)),
                              price_cents=row.price_cents, is_paid=None if engine_authored else False if row.price_cents else None,
                              automation_kind=kind,
                              sent_by_employee_id=human_employee_id(s) if kind is None else None))
                state.input_revision += 1
                s.flush()
            from .media_delivery import project
            project(s, row, proof.message_id, stamp)
            if row.quote_json:
                from .quote import RECONCILED
                row.quote_lifecycle = RECONCILED
            if receipt is None:
                receipt = OfMessageDelivery(account_id=account_id, fan_id=row.fan_id, message_id=proof.message_id)
                s.add(receipt)
            receipt.outcome, receipt.attempt_id = "sent", row.attempt_id
            receipt.origin = "ai" if row.origin in {"automated", "engagement"} else "human"
            receipt.evidence_ref, receipt.recorded_at = row.evidence_ref, stamp
            receipt.detail = "The transport accepted this exact message. This is not a purchase or read receipt."
            chat = s.get(Chat, (account_id, row.fan_id))
            if chat is None:
                chat = Chat(account_id=account_id, fan_id=row.fan_id, unread_count=0)
                s.add(chat)
            if chat.last_message_at is None or (stamp, proof.message_id) > (chat.last_message_at, chat.last_message_id or 0):
                chat.last_message_id, chat.last_message_at, chat.last_message_preview = proof.message_id, stamp, row.body[:500]
            if row.moxy_event_id:
                from moxy_replacement.runtime import delivered
                delivered(s, row)
            row.projected_at, row.updated_at = utcnow(), utcnow()
            from conversation_engine.delivery import completed as engine_completed
            engine_completed(s, row)
            self._release_hold(s, row, tenant_id)
            return True

    def dispatch(self, tenant_id, account_id, intent_id, transport):
        from .provider_connector import NativeProviderConnector
        from .fastt_manual import FasttManualTransport, fastt_owned
        from fans_api_adapter.synthetic_transport import FansApiSyntheticTransport
        from fans_api_adapter.local_http import FansApiLocalHttpTransport
        from fans_api_adapter.protocol_lab import FansApiProtocolLabTransport
        from fans_api_adapter.real_transport import FansApiRealTransport, real_transport_enabled
        # One sender per owned fan: only the FASTT runner's transport may send a
        # operator-authored manual/campaign message for it. Campaign origin
        # retains automated-history and campaign-control validation.
        with Session(self.engine) as s:
            queued = s.get(OfTextIntent, (account_id, intent_id))
            if queued is not None:
                if type(transport) is FansApiRealTransport and str(queued.fan_id) not in transport.allowed_chats:
                    return 'refused'
                owned_fan = fastt_owned(account_id, queued.fan_id)
                is_fastt = type(transport) is FasttManualTransport
                if (is_fastt and not (owned_fan and queued.origin in {"manual", "campaign"})) or (owned_fan and not is_fastt):
                    return "queued"
        transport_generation = None
        if type(transport) is FasttManualTransport:
            transport_generation = transport.policy_revision
        elif type(transport) is FansApiRealTransport:
            if not real_transport_enabled():
                raise Refused("real Fans API transport is not enabled")
        elif type(transport) not in {SyntheticTextTransport, FansApiSyntheticTransport, FansApiLocalHttpTransport, FansApiProtocolLabTransport}:
            try:
                from .loopback_transport import LoopbackTextTransport
                from .native_connector import NativeLoopbackConnector
            except ImportError as exc:
                raise Refused("only an explicitly scoped registered transport is supported") from exc
            if type(transport) not in {LoopbackTextTransport, NativeLoopbackConnector, NativeProviderConnector}:
                raise Refused("only an explicitly scoped registered transport is supported")
            transport_generation = transport.policy_revision
        if transport.account_id != account_id:
            raise Refused("transport scope mismatch")
        with Session(self.engine) as s:
            self.store._delivery_runtime(s, tenant_id, account_id)
            policy = s.get(OfDeliveryPolicy, account_id)
            expected = ("native" if type(transport) in {NativeProviderConnector, FasttManualTransport}
                        else "fansapi" if type(transport) is FansApiRealTransport else "synthetic")
            if policy is None or policy.transport_kind != expected:
                raise Refused("transport does not match the account delivery mode")
        if type(transport) is FansApiRealTransport:
            # Restart recovery: an attempt left 'dispatching' with nobody behind it is UNKNOWN once past its
            # deadline (it may have reached the provider). It is never re-queued or retried from here.
            self.expire_dispatches(tenant_id, account_id, limit=32, synthetic_engine_only=True,
                                   allowed_fans=frozenset(map(int, transport.allowed_chats)))
        grant = self.admit(tenant_id, account_id, intent_id, transport_generation=transport_generation)
        if isinstance(grant, str):
            return grant
        try:
            proof = transport.send(grant, authority=self, tenant_id=tenant_id)
        except Exception as exc:
            auth_failed = ((getattr(exc, "reason", None) == "account-auth-failed")
                           or getattr(exc, "status_code", None) == 401
                           or type(exc).__name__ == "NativeAuthenticationError")
            throttled = (isinstance(exc, ProviderThrottled)
                         or getattr(exc, "status_code", None) == 429)
            if auth_failed or throttled:
                retry_after = getattr(exc, "retry_after", None)
                try:
                    self._account_cooldown(
                        tenant_id, account_id,
                        seconds=300 if auth_failed else (retry_after if retry_after is not None else 10))
                except Exception:
                    pass
                rejection = NotAccepted(
                    account_id=grant.account_id, fan_id=grant.fan_id, intent_id=grant.intent_id,
                    attempt_id=grant.attempt_id, payload_hash=grant.payload_hash,
                    occurred_at=datetime.now(timezone.utc).isoformat(),
                    evidence_ref=("fastt-auth-failed:" if auth_failed else "fastt-rate-limited:")
                    + grant.attempt_id)
                try:
                    return self.record_not_accepted(
                        tenant_id, account_id, intent_id, grant.token, rejection)
                except Exception:
                    pass
            # Exception text may contain credentials or customer data; log type and frames only.
            import sys, traceback
            frames = ",".join(f"{f.filename.rsplit('/', 1)[-1]}:{f.lineno}" for f in traceback.extract_tb(exc.__traceback__)[-6:])
            status = getattr(exc, "send_http_status", None)
            http = f" http_status={status}" if type(status) is int and 100 <= status <= 599 else ""
            print(f"onlyfans_dispatch_failed intent={intent_id} type={type(exc).__name__}{http} at={frames}", file=sys.stderr, flush=True)
            try:
                return self.mark_unknown(tenant_id, account_id, intent_id, grant.token)
            except Refused as refusal:
                if str(refusal) != "stale dispatch token":
                    raise
                # The token was invalidated (the attempt was proven never wired and retired): nothing to mark.
                with Session(self.engine) as s:
                    return self._intent(s, account_id, intent_id).status
        if type(proof) is NotAccepted:
            return self.record_not_accepted(tenant_id, account_id, intent_id, grant.token, proof)
        result = self.record_accepted(tenant_id, account_id, intent_id, grant.token, proof)
        if result == "accepted":
            self.materialize(tenant_id, account_id, intent_id)
        return result

    def _account_cooldown(self, tenant_id, account_id, *, seconds):
        """Stop this account's other text sends after a provider-wide refusal."""
        with Session(self.engine) as s, s.begin():
            self.store._runtime(s, tenant_id, account_id)
            policy = s.scalar(select(OfDeliveryPolicy).where(
                OfDeliveryPolicy.account_id == account_id).with_for_update())
            if policy is None:
                return
            now = self._db_now(s)
            # The provider's Retry-After is authoritative. Keep the same one-day
            # ceiling as the native path instead of shortening a valid long wait.
            until = now + timedelta(seconds=max(1, min(86400, int(seconds))))
            from . import concurrent_admission
            if concurrent_admission.mode(policy) == "concurrent":
                current = getattr(policy, "provider_cooldown_until", None)
                policy.provider_cooldown_until = max(current, until) if current is not None else until
            else:
                current = policy.next_dispatch_at
                policy.next_dispatch_at = max(current, until) if current is not None else until

    def expire_dispatches(self, tenant_id, account_id, *, limit=100, synthetic_engine_only=False, allowed_fans=None):
        if type(limit) is not int or not 1 <= limit <= 100 or type(synthetic_engine_only) is not bool:
            raise Refused("invalid recovery limit")
        if allowed_fans is not None and (not synthetic_engine_only or type(allowed_fans) is not frozenset or not allowed_fans
                or any(type(fan) is not int or not 0 < fan < 2**63 for fan in allowed_fans)):
            raise Refused("invalid recovery fan scope")
        with Session(self.engine) as s, s.begin():
            runtime = self._historical_runtime(s, tenant_id, account_id)
            now = self._db_now(s)
            query = select(OfTextIntent).where(OfTextIntent.account_id == account_id,
                OfTextIntent.status == "dispatching", OfTextIntent.deadline <= now)
            if allowed_fans is not None:
                query = query.where(OfTextIntent.fan_id.in_(allowed_fans))
            if synthetic_engine_only:
                from db.onlyfans_engine_models import OfEngineDecision
                policy = s.get(OfDeliveryPolicy, account_id)
                if policy is None or policy.transport_kind not in ENGINE_TRANSPORT_KINDS:
                    raise Refused('synthetic engine recovery scope unavailable')
                query = query.join(OfEngineDecision, (OfEngineDecision.account_id == OfTextIntent.account_id) &
                    (OfEngineDecision.intent_id == OfTextIntent.intent_id)).where(
                    OfTextIntent.binding_id == runtime.binding_id, OfEngineDecision.binding_id == runtime.binding_id)
            rows = s.scalars(query.order_by(OfTextIntent.deadline, OfTextIntent.intent_id).limit(limit)).all()
            for row in rows:
                # Only the engine-scoped form may prove "never wired" (wire marker absent under the lock).
                if not (synthetic_engine_only and self._cancel_never_wired(s, row, "prewire_crash_never_wired", tenant_id)):
                    self._hold_unknown(s, row, "dispatch_expired", tenant_id)
            return len(rows)

    def repair(self, tenant_id, account_id, *, limit=100, synthetic_engine_only=False, allowed_fans=None):
        """Project saved accepted receipts (network-free; this method cannot call a transport).

        ``allowed_fans`` is the bounded normal-worker-recovery form: only those fans of the current binding
        are repaired, and one receipt that cannot be projected (conflicting canonical identity, invalid saved
        evidence) stays held exactly as before without stopping the rest or the worker tick.
        """
        if type(limit) is not int or not 1 <= limit <= 100 or type(synthetic_engine_only) is not bool:
            raise Refused("invalid repair limit")
        if allowed_fans is not None and (not synthetic_engine_only or type(allowed_fans) is not frozenset or not allowed_fans
                or any(type(fan) is not int or not 0 < fan < 2**63 for fan in allowed_fans)):
            raise Refused("invalid repair fan scope")
        with Session(self.engine) as s, s.begin():
            runtime = self._historical_runtime(s, tenant_id, account_id)
            query = select(OfTextIntent.intent_id).where(OfTextIntent.account_id == account_id,
                OfTextIntent.status == "accepted", OfTextIntent.projected_at.is_(None))
            if allowed_fans is not None:
                query = query.where(OfTextIntent.fan_id.in_(allowed_fans))
            if synthetic_engine_only:
                from db.onlyfans_engine_models import OfEngineDecision
                policy = s.get(OfDeliveryPolicy, account_id)
                if policy is None or policy.transport_kind not in ENGINE_TRANSPORT_KINDS:
                    raise Refused('synthetic engine recovery scope unavailable')
                query = query.join(OfEngineDecision, (OfEngineDecision.account_id == OfTextIntent.account_id) &
                    (OfEngineDecision.intent_id == OfTextIntent.intent_id)).where(
                    OfTextIntent.binding_id == runtime.binding_id, OfEngineDecision.binding_id == runtime.binding_id)
            ids = s.scalars(query.order_by(OfTextIntent.created_at, OfTextIntent.intent_id).limit(limit)).all()
        repaired = 0
        for identity in ids:
            try:
                repaired += bool(self.materialize(tenant_id, account_id, identity, synthetic_engine_only=synthetic_engine_only))
            except Refused:
                if allowed_fans is None:
                    raise
        return repaired
