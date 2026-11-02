"""Durable scheduler and transactional seams for the common delivery authority.

Public methods take trusted tenant/account scope, acquired using Store._runtime.
Hooks take that already-locked runtime and the SAME Session as TextAuthority.
There is intentionally no sender and no fallback to manual/campaign origins.
"""
from datetime import timedelta
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session
from db.onlyfans_delivery_models import OfTextIntent
from ..media_delivery import catalog_terms, assert_unowned
from ..native_context import canonical
from ..native_sales import caption
from ..store import Refused, _id
from ..text_delivery import TextAuthority
from .contracts import Policy, digest, identity, intent_key, load_object
from .models import Action, Settings, SettingsChange
from . import readers

ACTIVE = ("scheduled", "queued", "dispatching", "unknown")


def invalidate(s, runtime, *, reason, actor="system", fan_id=None, now=None):
    """Record cancellation in the event writer's existing runtime transaction.

    Final dispatch still revalidates the snapshot. An attempt which has already
    started remains dispatching/unknown; cancellation cannot invent non-delivery.
    """
    from ..store import utcnow
    query = select(Action).where(Action.account_id == runtime.account_id,
        Action.binding_id == runtime.binding_id,
        Action.status.in_(ACTIVE))
    if fan_id is not None:
        query = query.where(Action.fan_id == fan_id)
    for row in s.scalars(query):
        EngagementService._cancel(s, row, now or utcnow(), actor, reason)


class EngagementService:
    def __init__(self, store):
        self.store, self.engine = store, store.engine
        self.authority = TextAuthority(store)

    def _settings(self, s, runtime):
        row = s.get(Settings, runtime.account_id)
        if row is None or row.binding_id != runtime.binding_id:
            raise Refused("engagement_disabled")
        change = s.get(SettingsChange, (row.account_id, row.revision))
        if change is None or change.binding_id != row.binding_id or change.settings_json != row.settings_json:
            raise Refused("engagement_settings_changed")
        policy = Policy.parse(load_object(row.settings_json))
        if not policy.enabled:
            raise Refused("engagement_disabled")
        return row, policy

    def configure(self, tenant, account, *, expected_revision, command_key, actor, settings):
        identity(command_key, actor)
        if type(expected_revision) is not int or expected_revision < 0:
            raise Refused("invalid engagement revision")
        policy = Policy.parse(settings)
        request = digest([expected_revision, actor, policy.json()])
        with Session(self.engine) as s, s.begin():
            runtime = self.store._runtime(s, tenant, account)
            prior = s.scalar(select(SettingsChange).where(SettingsChange.account_id == account,
                                                        SettingsChange.command_key == command_key))
            if prior:
                if prior.binding_id != runtime.binding_id or prior.request_hash != request:
                    raise Refused("idempotency key conflict")
                return prior.revision
            row = s.get(Settings, account)
            if (row.revision if row else 0) != expected_revision:
                raise Refused("engagement revision conflict")
            revision = expected_revision + 1
            now = self.authority._db_now(s)
            if row is None:
                row = Settings(account_id=account)
                s.add(row)
            row.binding_id, row.revision, row.settings_json = runtime.binding_id, revision, policy.json()
            s.add(SettingsChange(account_id=account, revision=revision, binding_id=runtime.binding_id,
                command_key=command_key, request_hash=request, settings_json=policy.json(), actor=actor, created_at=now))
            # Revision fencing immediately invalidates queued/granted work too.
            for action in s.scalars(select(Action).where(Action.account_id == account, Action.status.in_(ACTIVE))):
                self._cancel(s, action, now, actor, "settings_changed")
            return revision

    def _definition(self, s, runtime, fan_id, kind, now):
        from moxy_replacement.runtime import selected
        if selected(s,runtime.account_id):raise Refused('moxy_canonical_owner_required')
        settings, policy = self._settings(s, runtime)
        switches = {"offer": policy.offers, "follow_up": policy.follow_ups, "reengage": policy.reengagement}
        if kind not in switches or not switches[kind]:
            raise Refused("engagement_kind_disabled")
        fan, newest, inbound, fingerprint = readers.context(s, runtime, fan_id, now, self.authority)
        if newest.created_at < now - timedelta(days=90):
            raise Refused("conversation_too_old")
        terms, source = None, None
        if kind == "offer":
            readers.selling(s, runtime.account_id, fan, inbound, policy, now)
            shelf = readers.candidates(s, runtime.account_id, fan, policy, now)
            if not shelf:
                raise Refused("no_eligible_fixed_offer")
            terms = shelf[0]
            body, hours = caption(terms), policy.offer_delay_hours
        elif kind == "follow_up":
            source = readers.follow_source(s, runtime.account_id, fan_id, newest)
            body, hours = policy.follow_up_text, policy.follow_up_delay_hours
        else:
            body, hours = policy.reengage_text, policy.reengage_delay_hours
        due = newest.created_at + timedelta(hours=hours)
        # Deterministic window: repeated late scheduler ticks cannot extend it.
        expires = due + timedelta(hours=policy.expiry_hours)
        if expires <= now:
            raise Refused("engagement_window_expired")
        result = {"version": 1, "account": runtime.account_id, "binding": runtime.binding_id,
            "fan": fan_id, "kind": kind, "anchor": newest.message_id,
            "settings_revision": settings.revision, "context_hash": fingerprint,
            "terms": terms, "source": source, "body": body, "due": due, "expires": expires}
        if len(canonical(result).encode()) > 20000:
            raise Refused("engagement_snapshot_too_large")
        return result, due, expires

    def schedule(self, tenant, account, fan_id, *, kind, command_key, actor):
        _id(fan_id)
        identity(command_key, actor)
        if type(kind) is not str or kind not in {"offer", "follow_up", "reengage"}:
            raise Refused("invalid engagement kind")
        request = digest([fan_id, kind, actor])
        with Session(self.engine) as s, s.begin():
            runtime = self.store._runtime(s, tenant, account)
            previous = s.scalar(select(Action).where(Action.account_id == account, Action.command_key == command_key))
            if previous:
                if previous.binding_id != runtime.binding_id or previous.request_hash != request:
                    raise Refused("idempotency key conflict")
                return previous.action_id
            now = self.authority._db_now(s)
            definition, due, expires = self._definition(s, runtime, fan_id, kind, now)
            definition["actor"] = actor
            previous = s.scalar(select(Action).where(Action.account_id == account,
                Action.binding_id == runtime.binding_id, Action.fan_id == fan_id,
                Action.kind == kind, Action.anchor_message_id == definition["anchor"]))
            if previous:
                return previous.action_id
            for existing in s.scalars(select(Action).where(Action.account_id == account,
                    Action.fan_id == fan_id, Action.status.in_(ACTIVE))):
                self._refresh(s, runtime, existing, now)
                if existing.status in ACTIVE:
                    raise Refused("engagement_already_pending")
            s.flush()
            row = Action(account_id=account, action_id=str(uuid.uuid4()), binding_id=runtime.binding_id,
                fan_id=fan_id, kind=kind, anchor_message_id=definition["anchor"], command_key=command_key,
                request_hash=request, snapshot_json=canonical(definition), snapshot_hash=digest(definition),
                status="scheduled", due_at=due, expires_at=expires, created_at=now, updated_at=now)
            s.add(row)
            return row.action_id

    def _validate(self, s, runtime, row, now, *, own_intent=None, require_due=True):
        settings, policy = self._settings(s, runtime)
        data = load_object(row.snapshot_json)
        if (digest(data) != row.snapshot_hash or row.binding_id != runtime.binding_id
                or row.account_id != runtime.account_id
                or data["account"] != row.account_id or data["binding"] != row.binding_id
                or data["fan"] != row.fan_id or data["kind"] != row.kind
                or data["anchor"] != row.anchor_message_id
                or data["due"] != row.due_at.isoformat() or data["expires"] != row.expires_at.isoformat()
                or data["settings_revision"] != settings.revision or row.cancel_requested_at is not None):
            raise Refused("engagement_snapshot_changed")
        if now >= row.expires_at:
            raise Refused("engagement_expired")
        if require_due and now < row.due_at:
            raise Refused("engagement_not_due")
        fan, newest, inbound, fingerprint = readers.context(s, runtime, row.fan_id, now,
            self.authority, own_intent=own_intent)
        if fingerprint != data["context_hash"]:
            raise Refused("engagement_context_changed")
        if row.kind == "offer":
            readers.selling(s, row.account_id, fan, inbound, policy, now, own_intent=own_intent)
            terms = catalog_terms(s, row.account_id, data["terms"]["item"]["id"])
            assert_unowned(s, row.account_id, row.fan_id, terms)
            if terms != data["terms"]:
                raise Refused("engagement_catalogue_changed")
            # Cross-lane sends of the same media after scheduling also invalidate.
            if terms not in readers.candidates(s, row.account_id, fan, policy, now):
                raise Refused("engagement_offer_no_longer_eligible")
        elif row.kind == "follow_up":
            if canonical(readers.follow_source(s, row.account_id, row.fan_id, newest)) != canonical(data["source"]):
                raise Refused("engagement_follow_up_changed")
        if require_due:
            readers.frequency(s, row.account_id, row.fan_id, policy, now, own_intent=own_intent)
        return data

    @staticmethod
    def _cancel(s, row, now, actor, reason):
        if row.status not in ACTIVE:
            return
        row.cancel_requested_at = row.cancel_requested_at or now
        row.cancelled_by = row.cancelled_by or actor
        row.reason, row.updated_at = reason, now
        intent = s.get(OfTextIntent, (row.account_id, row.intent_id)) if row.intent_id else None
        if intent is not None:
            # TextAuthority alone releases reservations and unresolved holds.
            # Its prewire hook will refuse; after wire start cancellation cannot
            # prove non-delivery or permit another attempt.
            # A refusal is terminal before provider acceptance. The action
            # vocabulary calls this cancelled; retain its intent for evidence.
            row.status = "cancelled" if intent.status == "refused" else intent.status
        else:
            row.status = "cancelled"

    def cancel(self, tenant, account, action_id, *, actor, reason="operator_cancelled"):
        identity("0" * 64, actor)
        if reason not in {"operator_cancelled", "reply", "purchase", "takeover", "pause", "no_contact", "catalogue_changed"}:
            raise Refused("invalid cancellation reason")
        with Session(self.engine) as s, s.begin():
            runtime = self.store._runtime(s, tenant, account)
            row = self._action(s, runtime, action_id)
            self._cancel(s, row, self.authority._db_now(s), actor, reason)
            return self._view(row)

    @staticmethod
    def _action(s, runtime, action_id):
        row = s.get(Action, (runtime.account_id, action_id))
        if row is None or row.binding_id != runtime.binding_id:
            raise Refused("engagement action unavailable")
        return row

    def _refresh(self, s, runtime, row, now):
        if row.intent_id:
            intent = s.get(OfTextIntent, (row.account_id, row.intent_id))
            if intent is None:
                raise Refused("engagement intent link missing")
            row.status = "cancelled" if intent.status == "refused" else intent.status
            if intent.status == "refused" and row.cancel_requested_at is None:
                row.reason = intent.reason or "delivery_refused"
            row.updated_at = now
            return
        try:
            self._validate(s, runtime, row, now, require_due=False)
        except (Refused, KeyError, TypeError, ValueError) as exc:
            row.status = "expired" if now >= row.expires_at else "cancelled"
            row.reason, row.updated_at = str(exc)[:64], now

    def due(self, tenant, account):
        """Return one proposal. Re-reading it is safe; only atomic attach claims it.

        Workers prioritize reactive work before calling this method. A temporary
        frequency/history refusal leaves the schedule intact until expiry.
        """
        with Session(self.engine) as s, s.begin():
            runtime = self.store._runtime(s, tenant, account)
            now = self.authority._db_now(s)
            rows = s.scalars(select(Action).where(Action.account_id == account, Action.status.in_(ACTIVE))
                .order_by(Action.due_at, Action.action_id).limit(100)).all()
            for row in rows:
                self._refresh(s, runtime, row, now)
                if row.status != "scheduled" or row.due_at > now:
                    continue
                try:
                    data = self._validate(s, runtime, row, now)
                except Refused:
                    continue
                return {"action_id": row.action_id, "command_key": intent_key(row.action_id),
                        "fan_id": row.fan_id, "body": data["body"], "terms": data["terms"],
                        "actor": data["actor"], "expires_at": row.expires_at, "origin": "engagement"}
            return None

    def validate_prepare(self, s, runtime, *, action_id, fan_id, command_key, body, terms, now):
        """MUST run inside TextAuthority.prepare, holding its runtime lock."""
        row = self._action(s, runtime, action_id)
        if row.status != "scheduled" or row.intent_id is not None:
            raise Refused("engagement_already_consumed")
        data = self._validate(s, runtime, row, now)
        if (row.fan_id != fan_id or command_key != intent_key(action_id)
                or body != data["body"] or terms != data["terms"]):
            raise Refused("engagement_delivery_changed")
        return row

    def attach(self, s, runtime, row, intent, now):
        """Same transaction as intent creation; rollback removes BOTH writes."""
        if row.status != "scheduled" or row.intent_id is not None:
            raise Refused("engagement_already_consumed")
        data = self._validate(s, runtime, row, now, own_intent=intent)
        if (row.fan_id != intent.fan_id or intent.command_key != intent_key(row.action_id)
                or intent.body != data["body"] or
                (load_object(intent.catalog_snapshot_json) if intent.catalog_snapshot_json else None) != data["terms"]):
            raise Refused("engagement_delivery_changed")
        if (intent.account_id != row.account_id or intent.binding_id != row.binding_id
                or intent.origin != "engagement" or intent.actor != data["actor"]):
            raise Refused("engagement_intent_scope_changed")
        row.intent_id, row.status, row.updated_at = intent.intent_id, "queued", now

    def current(self, s, intent, runtime, now):
        """Required at BOTH admission and the final one-use wire marker."""
        if intent.origin != "engagement":
            return False
        row = s.scalar(select(Action).where(Action.account_id == intent.account_id,
                                           Action.intent_id == intent.intent_id))
        if row is None or row.status not in {"queued", "dispatching"}:
            return False
        try:
            data = self._validate(s, runtime, row, now, own_intent=intent)
            terms = data["terms"]
            return (row.fan_id == intent.fan_id and row.binding_id == intent.binding_id
                and intent.actor == data["actor"] and intent.status in {"queued", "dispatching"}
                and intent.command_key == intent_key(row.action_id) and intent.body == data["body"]
                and (load_object(intent.catalog_snapshot_json) if intent.catalog_snapshot_json else None) == terms
                and intent.catalog_item_id == (terms["item"]["id"] if terms else None)
                and intent.price_cents == (terms["price_cents"] if terms else 0)
                and load_object(intent.media_json) == (terms["media_ids"] if terms else [])
                and load_object(intent.preview_json) == (terms["preview_ids"] if terms else []))
        except (Refused, KeyError, TypeError, ValueError):
            return False

    @staticmethod
    def _view(row):
        return {"action_id": row.action_id, "kind": row.kind, "status": row.status,
                "reason": row.reason, "due_at": row.due_at, "expires_at": row.expires_at,
                "intent_id": row.intent_id, "cancel_requested": row.cancel_requested_at is not None}

    def detail(self, tenant, account, action_id):
        with Session(self.engine) as s, s.begin():
            runtime = self.store._runtime(s, tenant, account)
            row = self._action(s, runtime, action_id)
            if row.status in ACTIVE:
                self._refresh(s, runtime, row, self.authority._db_now(s))
            return self._view(row)

