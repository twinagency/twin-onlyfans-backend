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
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfDeliveryPolicyChange, OfTextIntent, OfNativePlan
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

    @staticmethod
    def _media_current(s, row, policy, now):
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
        if (type(expected_revision) is not int or expected_revision < 0 or type(enabled) is not bool
                or type(limit_units) is not int or not 0 <= limit_units <= 10000
                or type(throttle_seconds) is not int or not 1 <= throttle_seconds <= 86400
                or not isinstance(actor, str) or not 1 <= len(actor) <= 128
                or not isinstance(reason, str) or not 1 <= len(reason) <= 512
                or media_enabled is not None and type(media_enabled) is not bool
                or transport_kind is not None and transport_kind not in {"synthetic", "native"}):
            raise Refused("invalid delivery policy")
        with Session(self.engine) as s, s.begin():
            self.store._runtime(s, tenant_id, account_id)
            policy = s.get(OfDeliveryPolicy, account_id)
            if policy is None:
                if expected_revision != 0:
                    raise Refused("policy revision conflict")
                policy = OfDeliveryPolicy(account_id=account_id)
                s.add(policy)
                s.flush()
            if policy.revision != expected_revision:
                raise Refused("policy revision conflict")
            if transport_kind is not None and transport_kind != policy.transport_kind:
                if policy.active_intent_id or policy.queued_count:
                    raise Refused("delivery mode cannot change with active or queued work")
            before = self._policy_values(policy)
            if throttle_seconds > policy.throttle_seconds and policy.next_dispatch_at is not None:
                policy.next_dispatch_at = max(policy.next_dispatch_at,
                    self._db_now(s) + timedelta(seconds=throttle_seconds))
            policy.revision += 1
            policy.enabled, policy.limit_units, policy.throttle_seconds = enabled, limit_units, throttle_seconds
            if media_enabled is not None:
                policy.media_enabled = media_enabled
            if transport_kind is not None:
                policy.transport_kind = transport_kind
            # Policy changes never reset used units, holds, throttle or queue.
            s.add(OfDeliveryPolicyChange(account_id=account_id, revision=policy.revision,
                                         actor=actor, reason=reason, before_json=_json(before),
                                         after_json=_json(self._policy_values(policy)), created_at=utcnow()))
            return policy.revision

    @staticmethod
    def _retained_hold(s, account_id, fan_id):
        return s.scalar(select(OfMessageDelivery.message_id).where(
            OfMessageDelivery.account_id == account_id, OfMessageDelivery.fan_id == fan_id,
            OfMessageDelivery.outcome.in_(UNRESOLVED_OUTCOMES)).limit(1)) is not None

    def _eligibility(self, s, runtime, policy, state, fan, origin, now, *, own_attempt_id=None):
        if policy is None or not policy.enabled or policy.transport_kind not in {"synthetic", "native"}:
            return "delivery_disabled"
        if not runtime.enabled:
            return "account_disabled"
        if (runtime.recovery_needed or runtime.pending_events or runtime.last_sync_at is None
                or runtime.last_sync_at < now - timedelta(seconds=MAX_SYNC_AGE_SECONDS)
                or runtime.last_sync_at > now + timedelta(minutes=5)):
            from .store import fan_history_current
            if not (fan is not None and fan_history_current(s, runtime.account_id, fan.fan_id)):
                return "history_not_current"
        if s.scalar(select(OfIngress.event_key).where(
                OfIngress.account_id == runtime.account_id, OfIngress.status == "quarantined").limit(1)) is not None:
            return "history_quarantined"
        if state is None or fan is None:
            return "conversation_unavailable"
        if fan.is_muted or state.mode in {"paused", "no_contact"}:
            return "contact_blocked"
        if state.takeover_state == "requested":
            return "takeover_pending"
        if origin in {"automated", "campaign", "engagement"} and (state.mode != "automated" or state.takeover_state != "none"):
            return "owner_changed"
        if origin == "manual" and (state.mode != "human_controlled" or state.takeover_state not in {"none", "effective"}):
            return "human_control_required"
        if (state.unresolved_attempt_id is not None and state.unresolved_attempt_id != own_attempt_id) or self._retained_hold(s, runtime.account_id, fan.fan_id):
            return "unresolved_delivery"
        return None

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
                catalog_item_id=None, quoted_price_cents=None, campaign_id=None, engagement_action_id=None):
        _id(fan_id)
        if (not isinstance(command_key, str) or not 1 <= len(command_key) <= 64
                or type(expected_revision) is not int or expected_revision < 0
                or not isinstance(body, str) or not body.strip() or len(body.encode()) > 20000
                or origin not in {"automated", "manual", "campaign", "engagement"}
                or not isinstance(actor, str) or not 1 <= len(actor) <= 128
                or type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 600):
            raise Refused("invalid text intent")
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
            if origin == "automated" and native_plan_id is None:
                raise Refused("automated media requires its native plan")
        if quoted_price_cents is not None and (catalog_item_id is None or native_plan_id is None):
            raise Refused("only a recorded native quote may change the sticker price")
        request = dict(account_id=account_id, fan_id=fan_id, command_key=command_key,
                       expected_revision=expected_revision, body=body, origin=origin,
                       actor=actor, work=asdict(work) if work else None, ttl_seconds=ttl_seconds)
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
        fingerprint = hashlib.sha256(_json(request).encode()).hexdigest()
        with Session(self.engine) as s, s.begin():
            runtime = self.store._runtime(s, tenant_id, account_id)
            now = self._db_now(s)
            prior = s.scalar(select(OfTextIntent).where(OfTextIntent.account_id == account_id,
                                                       OfTextIntent.command_key == command_key))
            if prior is not None:
                if prior.command_hash != fingerprint:
                    raise Refused("idempotency key conflict")
                return prior.intent_id
            policy = s.get(OfDeliveryPolicy, account_id)
            state = s.get(OfConversationState, (account_id, fan_id))
            fan = s.get(Fan, (account_id, fan_id))
            reason = self._eligibility(s, runtime, policy, state, fan, origin, now)
            if reason:
                raise Refused(reason)
            from .handover import staff_owns_takeover
            if origin == "manual" and not staff_owns_takeover(s, account_id, fan_id, state, actor):
                raise Refused("takeover belongs to another team member")
            if state.revision != expected_revision:
                raise Refused("control revision conflict")
            if policy.queued_count >= MAX_QUEUED:
                raise Refused("delivery queue full")
            if policy.active_intent_id:
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
            if catalog_item_id is not None:
                from .media_delivery import catalog_terms, assert_unowned
                if not policy.media_enabled:
                    raise Refused("media delivery disabled")
                terms = catalog_terms(s, account_id, catalog_item_id, price_cents=quoted_price_cents)
                assert_unowned(s, account_id, fan_id, terms)
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
            if origin == "automated":
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
                               native_plan_id=native_plan_id, catalog_item_id=catalog_item_id,
                               catalog_snapshot_json=_json(terms) if terms else None,
                               media_json=_json(terms["media_ids"]) if terms else "[]",
                               preview_json=_json(terms["preview_ids"]) if terms else "[]",
                               price_cents=terms["price_cents"] if terms else 0)
            s.add(intent)
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
        with Session(self.engine) as s, s.begin():
            runtime = self.store._runtime(s, tenant_id, account_id)
            now = self._db_now(s)
            intent = self._intent(s, account_id, intent_id)
            if intent.status != "queued":
                if intent.status == "dispatching" and intent.wire_version == 0:
                    self._hold_unknown(s, intent, "legacy_dispatch_unverified")
                return intent.status
            policy = s.get(OfDeliveryPolicy, account_id)
            state = s.get(OfConversationState, (account_id, intent.fan_id))
            fan = s.get(Fan, (account_id, intent.fan_id))
            work = s.get(OfWork, (account_id, intent.fan_id)) if intent.work_generation is not None else None
            obsolete = (intent.wire_version != 1 or intent.expires_at <= now or state is None
                        or intent.binding_id != runtime.binding_id
                        or intent.control_revision != state.revision
                        or intent.input_revision != state.input_revision
                        or intent.payload_hash != self._intent_hash(intent)
                        or not self._media_current(s, intent, policy, now)
                        or policy is None or intent.policy_revision != policy.revision
                        or (transport_generation is not None and transport_generation != intent.policy_revision)
                        or (intent.work_generation is not None and
                            (work is None or work.generation != intent.work_generation)))
            from .handover import staff_owns_takeover
            if not obsolete and intent.origin == "manual":
                obsolete = not staff_owns_takeover(s, account_id, intent.fan_id, state, intent.actor)
            if not obsolete and intent.native_plan_id is not None:
                from .native_context import matches
                obsolete = not matches(s, intent)
            if not obsolete and intent.origin == "campaign":
                from .campaigns import current
                obsolete = not current(s, intent, runtime, now)
            if not obsolete and intent.origin == "engagement":
                obsolete = not self._engagement().current(s, intent, runtime, now)
            reason = "snapshot_changed" if obsolete else self._eligibility(s, runtime, policy, state, fan, intent.origin, now)
            if reason:
                intent.status = "cancelled" if obsolete else "refused"
                intent.reason, intent.updated_at = reason, now
                if policy is not None:
                    policy.queued_count -= 1
                return intent.status
            if policy.active_intent_id:
                intent.reason = "account_busy"
                return "queued"
            if policy.next_dispatch_at is not None and policy.next_dispatch_at > now:
                intent.reason = "account_throttled"
                return "queued"
            if policy.used_units >= policy.limit_units:
                intent.status, intent.reason, intent.updated_at = "refused", "cap_exhausted", now
                policy.queued_count -= 1
                return intent.status
            token, attempt = str(uuid.uuid4()), str(uuid.uuid4())
            policy.used_units += 1
            policy.queued_count -= 1
            policy.active_intent_id = intent.intent_id
            policy.next_dispatch_at = now + timedelta(seconds=policy.throttle_seconds)
            intent.status, intent.reason, intent.updated_at = "dispatching", None, now
            intent.attempt_id, intent.dispatch_token = attempt, token
            intent.dispatched_at, intent.deadline = now, now + timedelta(seconds=DISPATCH_SECONDS)
            intent.reservation_held = True
            state.unresolved_attempt_id, state.updated_at = attempt, now
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
                  (grant.token, 36), (grant.body, 20000), (grant.payload_hash, 64))
        if (any(type(value) is not str or not 1 <= len(value.encode()) <= limit for value, limit in fields)
                or len(grant.payload_hash) != 64 or any(c not in "0123456789abcdef" for c in grant.payload_hash)
                or type(grant.origin) is not str or grant.origin not in {"automated", "manual", "campaign", "engagement"}
                or type(grant.deadline) is not datetime or grant.deadline.tzinfo is not None
                or type(grant.policy_revision) is not int or grant.policy_revision < 0
                or type(grant.wire_version) is not int or grant.wire_version not in {0, 1}):
            raise Refused("invalid dispatch grant")
        if (type(grant.media_ids) is not tuple or type(grant.preview_ids) is not tuple
                or len(grant.media_ids) > 35
                or any(type(v) is not int or not 0 < v < 2**63 for v in grant.media_ids)
                or len(set(grant.media_ids)) != len(grant.media_ids)
                or any(type(v) is not int or v not in grant.media_ids for v in grant.preview_ids)
                or len(set(grant.preview_ids)) != len(grant.preview_ids)
                or type(grant.price_cents) is not int
                or (not grant.media_ids and (grant.preview_ids or grant.price_cents != 0))
                or (grant.media_ids and not 300 <= grant.price_cents <= 20000)):
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
            self.store._runtime(s, tenant_id, grant.account_id)
            row = self._intent(s, grant.account_id, grant.intent_id)
            now = self._db_now(s)
            if (self._grant(row) != grant or row.status != "dispatching" or row.wire_started_at is not None
                    or row.wire_version != 1 or row.deadline <= now or row.expires_at <= now
                    or row.policy_revision != transport_generation):
                raise Refused("dispatch is not eligible for identity preflight")

    def consume_dispatch(self, tenant_id, grant: Dispatch, *, transport_generation=None):
        """Commit the final one-use network marker, never an I/O transaction."""
        self._validate_grant(grant)
        if transport_generation is not None and (type(transport_generation) is not int or transport_generation < 0):
            raise Refused("invalid transport generation")
        with Session(self.engine) as s, s.begin():
            runtime = self._historical_runtime(s, tenant_id, grant.account_id)
            row = self._intent(s, grant.account_id, grant.intent_id)
            if self._grant(row) != grant:
                raise Refused("dispatch grant mismatch")
            if row.wire_started_at is not None or row.status != "dispatching":
                raise Refused("dispatch grant was already consumed or settled")
            now = self._db_now(s)
            if row.wire_version != 1 or row.deadline <= now or row.expires_at <= now:
                self._hold_unknown(s, row, "legacy_dispatch_unverified" if row.wire_version != 1 else "dispatch_expired")
                return None
            policy = s.get(OfDeliveryPolicy, row.account_id)
            state = s.get(OfConversationState, (row.account_id, row.fan_id))
            fan = s.get(Fan, (row.account_id, row.fan_id))
            binding = s.get(OfCreatorBinding, row.binding_id)
            work = s.get(OfWork, (row.account_id, row.fan_id)) if row.work_generation is not None else None
            obsolete = (binding is None or binding.id != runtime.binding_id or binding.revoked_at is not None
                        or state is None or state.revision != row.control_revision or state.input_revision != row.input_revision
                        or policy is None or policy.revision != row.policy_revision
                        or (transport_generation is not None and transport_generation != row.policy_revision)
                        or row.payload_hash != self._intent_hash(row)
                        or not self._media_current(s, row, policy, now)
                        or (row.work_generation is not None and (work is None or work.generation != row.work_generation)))
            from .handover import staff_owns_takeover
            if not obsolete and row.origin == "manual":
                obsolete = not staff_owns_takeover(s, row.account_id, row.fan_id, state, row.actor)
            if not obsolete and row.native_plan_id is not None:
                from .native_context import matches
                obsolete = not matches(s, row)
            if not obsolete and row.origin == "campaign":
                from .campaigns import current
                obsolete = not current(s, row, runtime, now)
            if not obsolete and row.origin == "engagement":
                obsolete = not self._engagement().current(s, row, runtime, now)
            reason = "snapshot_changed" if obsolete else self._eligibility(
                s, runtime, policy, state, fan, row.origin, now, own_attempt_id=row.attempt_id)
            if reason is None and (policy.active_intent_id != row.intent_id or not row.reservation_held
                                   or state.unresolved_attempt_id != row.attempt_id):
                reason = "attempt_ownership_changed"
            if reason is None and policy.used_units > policy.limit_units:
                reason = "cap_exhausted"
            if reason:
                if policy is None or (row.reservation_held and policy.used_units < 1):
                    raise Refused("delivery reservation unavailable")
                row.status = "cancelled" if obsolete else "refused"
                row.reason, row.updated_at = "prewire_" + reason, now
                if row.reservation_held:
                    policy.used_units -= 1
                    row.reservation_held = False
                self._release_hold(s, row)
                return None
            row.wire_started_at, row.updated_at = now, now
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
        self._historical_runtime(s, tenant_id, account_id)
        row = self._intent(s, account_id, intent_id)
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
    def _hold_unknown(s, row, reason):
        row.status, row.reason, row.updated_at = "unknown", reason, utcnow()
        state = s.get(OfConversationState, (row.account_id, row.fan_id))
        state.unresolved_attempt_id, state.requires_attention, state.updated_at = row.attempt_id, True, utcnow()
        policy = s.get(OfDeliveryPolicy, row.account_id)
        policy.active_intent_id = row.intent_id

    def mark_unknown(self, tenant_id, account_id, intent_id, dispatch_token, reason="ambiguous_transport"):
        if reason not in {"ambiguous_transport", "invalid_receipt", "dispatch_expired", "provider_identity_conflict"}:
            raise Refused("invalid uncertainty reason")
        with Session(self.engine) as s, s.begin():
            row = self._attempt(s, tenant_id, account_id, intent_id, dispatch_token)
            if row.status == "dispatching":
                self._hold_unknown(s, row, reason)
            return row.status

    def record_accepted(self, tenant_id, account_id, intent_id, dispatch_token, receipt):
        with Session(self.engine) as s, s.begin():
            row = self._attempt(s, tenant_id, account_id, intent_id, dispatch_token)
            if row.status not in {"dispatching", "unknown", "accepted"}:
                raise Refused("attempt is already settled")
            try:
                if type(receipt) is not Accepted:
                    raise Refused("invalid accepted evidence")
                _id(receipt.message_id)
                self._validate_proof(row, receipt)
                encoded = _json(asdict(receipt))
            except (Refused, TypeError, AttributeError):
                if row.status == "accepted":
                    raise Refused("accepted receipt conflict")
                self._hold_unknown(s, row, "invalid_receipt")
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
            row.receipt_json, row.evidence_ref = encoded, receipt.evidence_ref
            if other is not None:
                self._hold_unknown(s, row, "provider_identity_conflict")
                return "unknown"
            row.status, row.reason, row.updated_at = "accepted", None, utcnow()
            row.provider_message_id = receipt.message_id
            return "accepted"

    def _release_hold(self, s, row):
        policy = s.get(OfDeliveryPolicy, row.account_id)
        if row.wire_started_at is not None:
            floor = self._db_now(s) + timedelta(seconds=policy.throttle_seconds)
            policy.next_dispatch_at = max(policy.next_dispatch_at or floor, floor)
        if policy.active_intent_id == row.intent_id:
            policy.active_intent_id = None
        state = s.get(OfConversationState, (row.account_id, row.fan_id))
        if state.unresolved_attempt_id == row.attempt_id:
            state.unresolved_attempt_id = None
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
                self._hold_unknown(s, row, "invalid_receipt")
                return "unknown"
            if row.status == "not_accepted":
                if row.receipt_json != encoded:
                    raise Refused("rejection evidence conflict")
                return "not_accepted"
            row.status, row.reason, row.updated_at = "not_accepted", "proved_not_accepted", utcnow()
            row.receipt_json, row.evidence_ref = encoded, rejection.evidence_ref
            if row.reservation_held:
                policy = s.get(OfDeliveryPolicy, account_id)
                policy.used_units -= 1
                row.reservation_held = False
            self._release_hold(s, row)
            return row.status

    def materialize(self, tenant_id, account_id, intent_id):
        """Project a saved receipt atomically. This method cannot call transport."""
        with Session(self.engine) as s, s.begin():
            self._historical_runtime(s, tenant_id, account_id)
            row = self._intent(s, account_id, intent_id)
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
                s.add(Message(account_id=account_id, fan_id=row.fan_id, message_id=proof.message_id,
                              direction="out", body=row.body, created_at=stamp, raw_json=row.receipt_json,
                              media_ids=row.media_json, media_count=len(json.loads(row.media_json)),
                              price_cents=row.price_cents, is_paid=False if row.price_cents else None,
                              automation_kind=kind,
                              sent_by_employee_id=human_employee_id(s) if kind is None else None))
                state.input_revision += 1
                s.flush()
            from .media_delivery import project
            project(s, row, proof.message_id, stamp)
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
            row.projected_at, row.updated_at = utcnow(), utcnow()
            self._release_hold(s, row)
            return True

    def dispatch(self, tenant_id, account_id, intent_id, transport):
        from .provider_connector import NativeProviderConnector
        transport_generation = None
        if type(transport) is not SyntheticTextTransport:
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
            self.store._runtime(s, tenant_id, account_id)
            policy = s.get(OfDeliveryPolicy, account_id)
            expected = "native" if type(transport) is NativeProviderConnector else "synthetic"
            if policy is None or policy.transport_kind != expected:
                raise Refused("transport does not match the account delivery mode")
        grant = self.admit(tenant_id, account_id, intent_id, transport_generation=transport_generation)
        if isinstance(grant, str):
            return grant
        try:
            proof = transport.send(grant, authority=self, tenant_id=tenant_id)
        except Exception as exc:
            # Type, message and frame locations only; never request bodies or credentials.
            import sys, traceback
            frames = ",".join(f"{f.filename.rsplit('/', 1)[-1]}:{f.lineno}" for f in traceback.extract_tb(exc.__traceback__)[-6:])
            print(f"onlyfans_dispatch_failed intent={intent_id} type={type(exc).__name__} msg={str(exc)[:200]!r} at={frames}", file=sys.stderr, flush=True)
            return self.mark_unknown(tenant_id, account_id, intent_id, grant.token)
        if type(proof) is NotAccepted:
            return self.record_not_accepted(tenant_id, account_id, intent_id, grant.token, proof)
        result = self.record_accepted(tenant_id, account_id, intent_id, grant.token, proof)
        if result == "accepted":
            self.materialize(tenant_id, account_id, intent_id)
        return result

    def expire_dispatches(self, tenant_id, account_id, *, limit=100):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise Refused("invalid recovery limit")
        with Session(self.engine) as s, s.begin():
            self._historical_runtime(s, tenant_id, account_id)
            now = self._db_now(s)
            rows = s.scalars(select(OfTextIntent).where(OfTextIntent.account_id == account_id,
                OfTextIntent.status == "dispatching", OfTextIntent.deadline <= now)
                .order_by(OfTextIntent.deadline, OfTextIntent.intent_id).limit(limit)).all()
            for row in rows:
                self._hold_unknown(s, row, "dispatch_expired")
            return len(rows)

    def repair(self, tenant_id, account_id, *, limit=100):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise Refused("invalid repair limit")
        with Session(self.engine) as s, s.begin():
            self._historical_runtime(s, tenant_id, account_id)
            ids = s.scalars(select(OfTextIntent.intent_id).where(OfTextIntent.account_id == account_id,
                OfTextIntent.status == "accepted", OfTextIntent.projected_at.is_(None))
                .order_by(OfTextIntent.created_at, OfTextIntent.intent_id).limit(limit)).all()
        return sum(self.materialize(tenant_id, account_id, identity) for identity in ids)
