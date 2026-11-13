"""Default-off supervised Plan C worker using the existing delivery authority.

Constructing this worker requires a durable, explicitly active single-pilot
scope. Tests inject mock HTTP transports; no configuration here activates a
policy, approves a draft, creates credentials, or starts a process.
"""
from dataclasses import dataclass, field
import json
from hashlib import sha256
import os
import re

from sqlalchemy import and_, func, or_, select, tuple_
from sqlalchemy.orm import Session, aliased

from conversation_engine import approval
from conversation_engine.journey import journey_config
from conversation_engine.model_config import ModelConfig
from conversation_engine.model_ledger import IDENTITY_UNSETTLED
from conversation_engine.pilot_scope import require, require_model, require_profile, policy_matches, _product_current
from conversation_engine.real_worker import RealConversationWorker
from conversation_engine.writer_context import JsonFileCreatorProfiles
from db.onlyfans_delivery_models import OfDeliveryPolicy
from db.onlyfans_engine_models import OfEngineApproval, OfEngineDecision, OfEngineModelCall, OfEnginePilotScope, OfEngineRequest, OfFansConnection, OfFansWebhook, OfEnginePilotWebhookDigest, OfEnginePilotReconnectReceipt
from db.onlyfans_fans_intake_models import OfFansProjection
from db.onlyfans_models import OfIngress
from fans_api_adapter.events import FansEventIntake, SendStopFailed, _parse as parse_event
from fans_api_adapter.real_transport import build_real_transport, load_real_config, real_transport_enabled
from fans_api_adapter.webhooks import verify_signature
from onlyfans_runtime.store import Refused, utcnow
from .pilot_draft import _NAME, _secret
from .wiring import build_engine_service


def _event_fan(raw):
    """(event, fan) for an engine-projected event with the documented shape; Refused for any other class."""
    from fans_api_adapter.event_scope import classify_raw, SUPPORTED
    verdict = classify_raw(raw)
    if verdict.cls != SUPPORTED:
        raise Refused('invalid pilot event')
    return verdict.event, verdict.fan


def _known_irrelevant(raw):
    from fans_api_adapter.event_scope import classify_raw, IRRELEVANT
    return classify_raw(raw).cls == IRRELEVANT


def _scope(raw, allowed_fans):
    """(outside_the_engine_path, hold) from the shared classification contract.

    Only a verified-irrelevant event, an event for a fan outside the pilot, or a supported event
    inside the pilot avoids a hold. A MATERIAL event holds only when it names a pilot fan (the
    hold is scoped to that fan when read); everything unknown or malformed holds the account.
    """
    try:
        _, fan = _event_fan(raw)
        return fan is not None and fan not in allowed_fans, False
    except Refused:
        pass
    if _known_irrelevant(raw):
        return True, False
    from fans_api_adapter.event_scope import classify_raw, MATERIAL
    verdict = classify_raw(raw)
    if verdict.cls == MATERIAL:
        return True, verdict.fan in allowed_fans
    return True, True


# Retained bytes for unresolved events live in the existing durable receipt table. Reconnects keep the
# earlier 1 MB bound; every other held event is bounded per event and per account.
RETAIN_MAX_BYTES = 65536
RETAIN_MAX_PER_ACCOUNT = 2000


def _record_outside_digest(store, tenant, account, raw, event_key, *, signature, secret, safety_hold=False):
    """Persist the digest and, when held, its authenticated receipt bytes atomically."""
    if not verify_signature(raw, signature, secret):
        raise Refused("webhook authentication failed")
    if type(event_key) is not str or not re.fullmatch(r'[\x21-\x7e]{1,255}', event_key):
        raise Refused('invalid webhook identity')
    digest = sha256(raw).hexdigest()
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        require(s, runtime)  # Retained intake evidence grants no model or send rights.
        active = s.scalar(select(OfFansConnection).where(OfFansConnection.account_id == account,
            OfFansConnection.binding_id == runtime.binding_id, OfFansConnection.status == 'active'))
        # A provider disconnect retires the active connection. Retain later
        # signed uncertainty against the latest binding epoch; this grants no
        # permission and does not revive the retired account.
        if active is None:
            active = s.scalar(select(OfFansConnection).where(OfFansConnection.account_id == account,
                OfFansConnection.binding_id == runtime.binding_id).order_by(OfFansConnection.epoch.desc()).limit(1))
        active_epoch = active.epoch if active is not None else None
        if active is None or active.binding_id != runtime.binding_id:
            raise Refused('pilot connection changed')
        # A reconnect may legitimately reuse event keys. Resolve a known provider account
        # to its own epoch; malformed/unknown envelopes are conservatively scoped to active.
        try:
            provider_account = json.loads(raw).get('account_id')
        except (ValueError, TypeError, AttributeError, UnicodeError, RecursionError):
            provider_account = None
        matched = s.scalar(select(OfFansConnection).where(
            OfFansConnection.account_id == account, OfFansConnection.binding_id == runtime.binding_id,
            OfFansConnection.provider_creator_id == active.provider_creator_id,
            OfFansConnection.provider_account_id == provider_account).order_by(OfFansConnection.epoch.desc()).limit(1))
        epoch = matched.epoch if matched is not None else active_epoch
        existing = s.get(OfFansWebhook, (account, epoch, event_key))
        conflict = (existing is not None and existing.payload_sha256 != digest or
            s.scalar(select(OfEnginePilotWebhookDigest.payload_sha256).where(
                OfEnginePilotWebhookDigest.account_id == account, OfEnginePilotWebhookDigest.epoch == epoch,
                OfEnginePilotWebhookDigest.event_key == event_key,
                OfEnginePilotWebhookDigest.payload_sha256 != digest).limit(1)) is not None)
        marker = s.get(OfEnginePilotWebhookDigest, (account, epoch, event_key, digest))
        if marker is None:
            # Append-only replay/conflict evidence has no arbitrary lifetime row cap.
            # No old rows are deleted or compacted. Storage/backup remains an operational gate.
            s.add(OfEnginePilotWebhookDigest(account_id=account, epoch=epoch, event_key=event_key,
                payload_sha256=digest, safety_hold=bool(safety_hold or conflict or matched is None), received_at=utcnow()))
            s.flush()
        # Retain the exact authenticated bytes of every held event so it can be recovered or reviewed
        # later. An unbound reconnect keeps its earlier receipt rule; other held events are size- and
        # count-bounded. This only retains evidence: it grants no model or send right and cannot
        # recreate bytes that were never kept.
        held = bool(safety_hold or conflict or matched is None)
        if held and (marker is None or marker.safety_hold):
            try:
                envelope = parse_event(raw)
            except Refused:
                envelope = {}
            reconnect = (safety_hold and matched is None and
                         envelope.get('event') in ('accounts.connected', 'accounts.reconnected'))
            key = (account, epoch, event_key, digest)
            if s.get(OfEnginePilotReconnectReceipt, key) is None and (reconnect or (
                    len(raw) <= RETAIN_MAX_BYTES and s.scalar(select(func.count()).select_from(
                        OfEnginePilotReconnectReceipt).where(
                        OfEnginePilotReconnectReceipt.account_id == account)) < RETAIN_MAX_PER_ACCOUNT)):
                s.add(OfEnginePilotReconnectReceipt(account_id=account, epoch=epoch, event_key=event_key,
                    payload_sha256=digest, raw_body=raw, signature=signature, received_at=utcnow()))
        return bool(conflict)


@dataclass(frozen=True)
class SupervisedPilot:
    worker: RealConversationWorker
    tenant: str
    account: str
    secret: bytes
    scope_revision: int
    scope_fingerprint: str
    allowed_fans: frozenset
    model_config: ModelConfig
    profiles: JsonFileCreatorProfiles
    ingress_cursor: dict = field(default_factory=dict, compare=False, repr=False)
    send_cursor: dict = field(default_factory=dict, compare=False, repr=False)

    def __repr__(self):
        return f'SupervisedPilot(account={self.account!r}, secret=<redacted>)'

    def validate(self):
        self.worker._gate(self.tenant, self.account)
        journey = journey_config()
        profile = self.profiles.get(self.account)
        with Session(self.worker.engine.store.engine) as s, s.begin():
            runtime = self.worker.engine.store._runtime(s, self.tenant, self.account)
            scope, config = require(s, runtime, active=True)
            policy = s.get(OfDeliveryPolicy, self.account)
            if (scope.revision != self.scope_revision or scope.fingerprint != self.scope_fingerprint
                    or frozenset(config['fans']) != self.allowed_fans
                    or self.worker.transport.allowed_chats != frozenset(map(str, self.allowed_fans))
                    or not policy_matches(policy, config, enabled=True)
                    or self.worker.engine.model.config != self.model_config
                    or journey.temporary_pause_reopen_hours is not None or journey.auto_backfill
                    or profile is None or profile.missing_inputs()
                    or (config.get('delivery_mode', 'full') != 'text_only' and not profile.offers_permitted)
                    or profile.approved_item_ids != (str(config['product']['id']),)):
                raise Refused('supervised pilot configuration changed')
            require_model(config, self.account, self.model_config)
            require_profile(config, profile)
            return scope.active_since

    def _record_outside_digest(self, raw, event_key, *, signature, safety_hold=False):
        return _record_outside_digest(self.worker.engine.store, self.tenant, self.account,
                                      raw, event_key, signature=signature, secret=self.secret, safety_hold=safety_hold)

    def handle(self, raw, signature, event_key, *, ingest_only=False):
        if type(ingest_only) is not bool:
            raise Refused('invalid intake mode')
        if type(raw) is not bytes or len(raw) > 1_000_000 or not verify_signature(raw, signature, self.secret):
            raise Refused('webhook authentication failed')
        # Stopping planning/sending must not discard receipts, corrections or
        # financial observations. A stale worker may ingest only under the
        # current durable account/fan binding; it cannot plan or dispatch.
        with Session(self.worker.engine.store.engine) as s:
            runtime = self.worker.engine.store._runtime(s, self.tenant, self.account, lock=False)
            _, config = require(s, runtime)
            allowed_fans = frozenset(config['fans'])
        # HTTP intake persists and projects receipt-time barriers only. The existing
        # durable poller owns generation/sending; no request-local background work.
        planning = False
        if not ingest_only:
            try:
                self.validate()
                planning = True
            except Refused:
                pass
        outside, malformed = _scope(raw, allowed_fans)
        if outside:
            if self._record_outside_digest(raw, event_key, signature=signature, safety_hold=malformed):
                return {'outcome':'quarantined', 'fenced':self.worker.engine.fence_conflicted_sources(self.tenant, self.account)}
            return {'outcome':'outside_pilot_scope'}
        if planning:
            self.worker.engine.store.drain(self.tenant, self.account)
            from conversation_engine.pilot_ingress import preserve_contact_stops
            preserve_contact_stops(self.worker.engine.store, self.tenant, self.account)
        if not planning:
            try:
                result = self.worker.intake.accept(raw, signature, self.secret, event_key,
                    expected_tenant=self.tenant, expected_account=self.account)
            except Refused as exc:
                if str(exc) not in ('authentication challenge recovery is unverified', 'provider account is not bound'):
                    raise
                conflict = self._record_outside_digest(raw, event_key, signature=signature, safety_hold=True)
                return {'outcome':'quarantined' if conflict else 'held', 'planning':'held'}
            if result.get('outcome') == 'quarantined':
                self.worker.engine.fence_conflicted_sources(self.tenant, self.account)
            self.worker.engine.store.drain(self.tenant, self.account)
            from conversation_engine.pilot_ingress import preserve_contact_stops
            preserve_contact_stops(self.worker.engine.store, self.tenant, self.account)
            return {**result, 'planning':'held'}
        try:
            result = self.worker.handle(self.tenant, self.account, raw_body=raw, signature=signature,
                                        secret=self.secret, event_key=event_key)
        except Refused as exc:
            if str(exc) not in ('authentication challenge recovery is unverified', 'provider account is not bound'):
                raise
            conflict = self._record_outside_digest(raw, event_key, signature=signature, safety_hold=True)
            return {'outcome':'quarantined' if conflict else 'held', 'planning':'held'}
        self.validate()
        return result

    def tick(self, *, limit=5):
        """Recover bounded durable work, then send only current explicitly approved decisions."""
        if type(limit) is not int or not 1 <= limit <= 10:
            raise Refused('invalid pilot worker batch')
        active_since = self.validate()
        if active_since is None:
            raise Refused('pilot activation boundary is unavailable')
        store, intake = self.worker.engine.store, self.worker.intake
        store.drain(self.tenant, self.account)
        from conversation_engine.pilot_ingress import preserve_contact_stops
        preserve_contact_stops(store, self.tenant, self.account)
        expired = self.worker.transport.recover_interrupted()
        self.worker.transport.recover_accepted()  # saved accepted receipt -> transcript, no POST
        from fans_api_adapter.event_scope import event_fan, ACCOUNT_EVENTS
        with Session(store.engine) as s:
            runtime = store._runtime(s, self.tenant, self.account, lock=False)
            fan_expr = event_fan(OfFansWebhook.event_type, OfFansWebhook.raw_body)
            malformed = or_(fan_expr.is_(None), ~fan_expr.op('~')('^[1-9][0-9]{0,18}$'))
            pending = s.execute(select(OfFansProjection.epoch, OfFansProjection.event_key, OfFansWebhook.raw_body)
                .join(OfFansWebhook, and_(OfFansWebhook.account_id == OfFansProjection.account_id,
                      OfFansWebhook.epoch == OfFansProjection.epoch, OfFansWebhook.event_key == OfFansProjection.event_key))
                .join(OfFansConnection, and_(OfFansConnection.account_id == OfFansWebhook.account_id,
                      OfFansConnection.epoch == OfFansWebhook.epoch))
                .where(OfFansProjection.account_id == self.account, OfFansProjection.status == 'pending',
                       OfFansConnection.binding_id == runtime.binding_id,
                       or_(OfFansWebhook.event_type != 'messages.received', OfFansWebhook.received_at >= active_since),
                       or_(OfFansWebhook.event_type.in_(ACCOUNT_EVENTS),
                           fan_expr.in_(tuple(map(str, self.allowed_fans))), malformed))
                .order_by(OfFansProjection.updated_at, OfFansProjection.event_key).limit(limit)).all()
        projected = 0
        for epoch, key, raw in pending:
            self.validate()
            try:
                _, fan = _event_fan(raw)
                if fan is None or fan in self.allowed_fans:
                    intake._project(self.account, epoch, key)
                    projected += 1
            except SendStopFailed:
                raise  # the safety stop did not apply: loud, never re-projected as ordinary malformed input
            except Refused:
                # Authenticated but malformed input is held by the intake, never projected into another fan.
                intake._project(self.account, epoch, key)
        # Reclassify pending human-send observations before any approved dispatch.
        for fan in sorted(self.allowed_fans):
            self.validate()
            intake.drive(self.account, fan, fan_scoped=True)
            intake.classify_sent(self.account, fan)
        # Invalidating an approval can happen before a dispatcher sees its queued
        # intent. Retire that unsent intent through the existing queue accounting;
        # do not leave it outside both the send and regeneration scans.
        with Session(store.engine) as s:
            runtime = store._runtime(s, self.tenant, self.account, lock=False)
            invalid_queued = s.execute(select(OfEngineDecision.decision_id, OfEngineDecision.intent_id).join(
                OfEngineApproval, and_(OfEngineApproval.account_id == OfEngineDecision.account_id,
                    OfEngineApproval.request_id == OfEngineDecision.request_id,
                    OfEngineApproval.generation == OfEngineDecision.generation)).where(
                OfEngineDecision.account_id == self.account, OfEngineDecision.binding_id == runtime.binding_id,
                OfEngineDecision.fan_id.in_(self.allowed_fans), OfEngineDecision.status == 'queued',
                OfEngineDecision.intent_id.is_not(None),
                OfEngineApproval.authorization_mode == 'autonomous',
                OfEngineApproval.status.in_(('invalidated', 'expired'))).distinct().limit(limit)).all()
        for decision_id, intent_id in invalid_queued:
            self.validate()
            reason = self.worker._guard(self.tenant, self.account, decision_id)
            if reason:
                self.worker._cancel_queued(self.tenant, self.account, intent_id, reason)
        # Expire model leases through the existing ledger. A reservation that never
        # reached the wire can resume; a calling lease becomes unknown and stays held.
        with Session(store.engine) as s:
            runtime = store._runtime(s, self.tenant, self.account, lock=False)
            expired_calls = s.execute(select(OfEngineModelCall.request_id, OfEngineModelCall.generation).where(
                OfEngineModelCall.account_id == self.account,
                OfEngineModelCall.binding_id == runtime.binding_id,
                OfEngineModelCall.fan_id.in_(self.allowed_fans),
                OfEngineModelCall.created_at >= active_since,
                OfEngineModelCall.status.in_(('reserved', 'calling')),
                OfEngineModelCall.lease_expires_at <= utcnow()).order_by(
                    OfEngineModelCall.lease_expires_at, OfEngineModelCall.call_id).limit(limit)).all()
        for request_id, generation in expired_calls:
            self.validate()
            self.worker.engine.model.ledger.sweep(self.account, request_id, generation)
        # Only scope-era inbound messages without a completed draft are eligible
        # for repair. Unknown model calls are retained, never automatically retried.
        with Session(store.engine) as s:
            runtime = store._runtime(s, self.tenant, self.account, lock=False)
            scope, _ = require(s, runtime, active=True)
            request = OfEngineRequest
            existing = select(OfEngineApproval.request_id).where(
                OfEngineApproval.account_id == request.account_id,
                OfEngineApproval.request_id == request.request_id).exists()
            # Revisit only locally repairable, autonomous drafts. This is scheduling eligibility,
            # never send authority; the worker rechecks all live controls and creates a new generation.
            dead_auto = select(OfEngineApproval.request_id).where(
                OfEngineApproval.account_id == request.account_id,
                OfEngineApproval.request_id == request.request_id,
                OfEngineApproval.binding_id == runtime.binding_id,
                OfEngineApproval.authorization_mode == 'autonomous',
                OfEngineApproval.status.in_(('invalidated', 'expired')),
                OfEngineApproval.invalidation_reason.in_(
                    ('memory_changed', 'new_inbound_pending', 'pilot_contact_review_pending', 'unsent_cancelled', 'ttl'))).exists()
            ready = select(OfEngineDecision.decision_id).where(
                OfEngineDecision.account_id == request.account_id,
                OfEngineDecision.request_id == request.request_id,
                OfEngineDecision.binding_id == runtime.binding_id,
                OfEngineDecision.part_index == 0, OfEngineDecision.status == 'ready').exists()
            # A released not_sent / HTTP-status call is a proven zero-cost end: nothing is in flight and no usage is
            # unknown. Its request is eligible again once due (`retry_not_before`, set by the ledger for retryable
            # availability refusals); the runtime then retries within its attempt limit, or hands a non-retryable
            # refusal to a human. Unknown, in-flight and identity-unsettled calls stay held exactly as before.
            ledger = getattr(getattr(getattr(self.worker, 'engine', None), 'model', None), 'ledger', None)
            due_now = ledger.clock() if ledger is not None else utcnow()
            proven_refusal = or_(OfEngineModelCall.outcome == 'not_sent',
                                 OfEngineModelCall.outcome.op('~')('^http_[0-9]{3}$'))
            uncertain = select(OfEngineModelCall.call_id).where(
                OfEngineModelCall.account_id == request.account_id,
                OfEngineModelCall.request_id == request.request_id,
                or_(OfEngineModelCall.status.in_(('reserved','calling','unknown')),
                    and_(OfEngineModelCall.status == 'released', ~func.coalesce(proven_refusal, False),
                         or_(OfEngineModelCall.outcome.is_(None), OfEngineModelCall.outcome != 'lease_expired',
                             OfEngineModelCall.wire_started_at.is_not(None))),
                    and_(OfEngineModelCall.status == 'released', OfEngineModelCall.retry_not_before > due_now),
                    OfEngineModelCall.outcome.in_((*IDENTITY_UNSETTLED,'identity_unverified')))).exists()
            query = (select(OfIngress, request).outerjoin(request, and_(
                request.account_id == OfIngress.account_id, request.fan_id == OfIngress.fan_id,
                request.source_message_id == OfIngress.message_id, request.binding_id == runtime.binding_id))
                .where(OfIngress.account_id == self.account, OfIngress.binding_id == runtime.binding_id,
                    OfIngress.fan_id.in_(self.allowed_fans), OfIngress.source != 'import',
                    OfIngress.status.in_(('pending','applied')),
                    # Explicit renewal can repair already-open work from this pilot.
                    # Intake from before activation without a pilot request remains inert.
                    or_(OfIngress.received_at >= active_since,
                        and_(request.request_id.is_not(None), request.created_at >= scope.created_at,
                             request.status == 'open')),
                    or_(request.request_id.is_(None), and_(request.status == 'open',
                        or_(and_(~existing, or_(ready, ~uncertain)), and_(dead_auto, ~uncertain))))))
            cursor = self.ingress_cursor.get('last')
            if cursor is not None:
                query = query.where(tuple_(OfIngress.received_at, OfIngress.event_key) > tuple_(*cursor))
            rows = s.execute(query.order_by(OfIngress.received_at, OfIngress.event_key).limit(limit)).all()
            if rows:
                self.ingress_cursor['last'] = (rows[-1][0].received_at, rows[-1][0].event_key)
            else:
                self.ingress_cursor.clear()
            work = []
            for incoming, request in rows:
                decision = None if request is None else s.scalar(select(OfEngineDecision).where(
                    OfEngineDecision.account_id == self.account, OfEngineDecision.request_id == request.request_id,
                    OfEngineDecision.binding_id == runtime.binding_id, OfEngineDecision.part_index == 0,
                    OfEngineDecision.status == 'ready').order_by(OfEngineDecision.generation.desc()).limit(1))
                if request is not None:
                    latest_approval = s.scalar(select(OfEngineApproval).where(
                        OfEngineApproval.account_id == self.account,
                        OfEngineApproval.request_id == request.request_id).order_by(
                            OfEngineApproval.generation.desc(), OfEngineApproval.version.desc()).limit(1))
                    if latest_approval is not None:
                        if (latest_approval.authorization_mode != 'autonomous'
                                or latest_approval.status not in ('invalidated', 'expired')
                                or latest_approval.invalidation_reason not in (
                                    'memory_changed', 'new_inbound_pending', 'pilot_contact_review_pending', 'unsent_cancelled', 'ttl')
                                ):
                            continue
                        parts = s.scalars(select(OfEngineDecision).where(
                            OfEngineDecision.account_id == self.account,
                            OfEngineDecision.request_id == request.request_id,
                            OfEngineDecision.generation == latest_approval.generation)).all()
                        from conversation_engine.engine import _latest_generation, _proved_unsent
                        latest_parts = _latest_generation(s, self.account, request.request_id)
                        if (not parts or not latest_parts or latest_parts[0].generation != latest_approval.generation
                                or not (all(part.status == 'ready' and part.intent_id is None for part in parts)
                                        or _proved_unsent(s, self.account, parts))):
                            continue
                        # turn() retires a ready draft or uses the engine's existing
                        # proof of cancellation before regenerating. It cannot replay it.
                        decision = None
                work.append(('stage', incoming.fan_id, decision.decision_id) if decision is not None
                            else ('turn', incoming.fan_id, incoming.message_id))
        planned = []
        for action, fan, ident in work:
            self.validate()
            result = (self.worker.turn(self.tenant, self.account, fan, ident) if action == 'turn'
                      else self.worker._send(self.tenant, self.account, ident))
            planned.append(result.get('outcome'))
        with Session(store.engine) as s:
            runtime = store._runtime(s, self.tenant, self.account, lock=False)
            scope, config = require(s, runtime, active=True)
            automatic = []
            if config['authorization_mode'] == 'autonomous':
                automatic = list(s.execute(select(OfEngineApproval.request_id, OfEngineApproval.generation,
                    OfEngineApproval.version).where(OfEngineApproval.account_id == self.account,
                    OfEngineApproval.binding_id == runtime.binding_id, OfEngineApproval.fan_id.in_(self.allowed_fans),
                    OfEngineApproval.authorization_mode == 'autonomous', OfEngineApproval.status == 'awaiting',
                    OfEngineApproval.created_at >= active_since, OfEngineApproval.expires_at > utcnow())
                    .order_by(OfEngineApproval.created_at).limit(limit)))
        for request_id, generation, version in automatic:
            self.validate()
            try:
                approval.authorize_automatic(store, self.tenant, self.account, request_id, generation,
                                             expected_version=version)
            except Refused:
                pass
        with Session(store.engine) as s:
            runtime = store._runtime(s, self.tenant, self.account, lock=False)
            pending_part = aliased(OfEngineDecision)
            query = select(OfEngineDecision.decision_id, OfEngineApproval.created_at, OfEngineApproval.request_id).join(OfEngineApproval, and_(
                OfEngineApproval.account_id == OfEngineDecision.account_id,
                OfEngineApproval.request_id == OfEngineDecision.request_id,
                OfEngineApproval.generation == OfEngineDecision.generation)).where(
                OfEngineDecision.account_id == self.account, OfEngineDecision.binding_id == runtime.binding_id,
                OfEngineDecision.fan_id.in_(self.allowed_fans), OfEngineDecision.part_index == 0,
                OfEngineApproval.status == 'approved',
                select(pending_part.decision_id).where(
                    pending_part.account_id == OfEngineApproval.account_id,
                    pending_part.request_id == OfEngineApproval.request_id,
                    pending_part.generation == OfEngineApproval.generation,
                    pending_part.status.in_(('ready','queued'))).exists(),
                OfEngineApproval.expires_at > utcnow()).order_by(OfEngineApproval.created_at, OfEngineApproval.request_id)
            cursor = self.send_cursor.get('last')
            page = query.where(tuple_(OfEngineApproval.created_at, OfEngineApproval.request_id) > tuple_(*cursor)) if cursor else query
            rows = s.execute(page.limit(limit)).all()
            if not rows and cursor:
                self.send_cursor.clear()
                rows = s.execute(query.limit(limit)).all()
            if rows:
                self.send_cursor['last'] = (rows[-1][1], rows[-1][2])
            decisions = [row[0] for row in rows]
        dispatched = []
        for decision in decisions:
            self.validate()
            dispatched.append(self.worker.send_approved(self.tenant, self.account, decision)['outcome'])
        return {'expired':expired, 'projected':projected, 'planned':planned, 'dispatched':dispatched}


@dataclass(frozen=True)
class IntakeOnlyPilot:
    """Paused startup retains evidence; it has no model or sending transport."""
    store: object
    tenant: str
    account: str
    secret: bytes = field(repr=False)

    def handle(self, raw, signature, event_key, *, ingest_only=False):
        if type(ingest_only) is not bool:
            raise Refused('invalid intake mode')
        if type(raw) is not bytes or len(raw)>1_000_000 or not verify_signature(raw,signature,self.secret):
            raise Refused('webhook authentication failed')
        with Session(self.store.engine) as s:
            runtime=self.store._runtime(s,self.tenant,self.account,lock=False)
            _,config=require(s,runtime)
        outside,malformed=_scope(raw,frozenset(config['fans']))
        if outside:
            conflict=_record_outside_digest(self.store,self.tenant,self.account,raw,event_key,signature=signature,secret=self.secret,safety_hold=malformed)
            return {'outcome':'quarantined' if conflict else 'outside_pilot_scope','planning':'held'}
        try:
            result=FansEventIntake(self.store).accept(raw,signature,self.secret,event_key,
                expected_tenant=self.tenant,expected_account=self.account)
        except Refused as exc:
            if str(exc) not in ('authentication challenge recovery is unverified','provider account is not bound'):raise
            conflict=_record_outside_digest(self.store,self.tenant,self.account,raw,event_key,signature=signature,secret=self.secret,safety_hold=True)
            return {'outcome':'quarantined' if conflict else 'held','planning':'held'}
        self.tick()
        return {**result,'planning':'held'}

    def tick(self, *, limit=5):
        if type(limit) is not int or not 1<=limit<=10:raise Refused('invalid intake batch')
        with Session(self.store.engine) as s:
            runtime=self.store._runtime(s,self.tenant,self.account,lock=False)
            require(s,runtime)
            pending=s.execute(select(OfFansProjection.epoch,OfFansProjection.event_key).join(OfFansConnection,
                (OfFansConnection.account_id==OfFansProjection.account_id)&(OfFansConnection.epoch==OfFansProjection.epoch))
                .where(OfFansProjection.account_id==self.account,OfFansProjection.status=='pending',
                    OfFansConnection.binding_id==runtime.binding_id).order_by(OfFansProjection.updated_at).limit(limit)).all()
        intake=FansEventIntake(self.store)
        for epoch,key in pending:intake._project(self.account,epoch,key)
        self.store.drain(self.tenant,self.account)
        from conversation_engine.pilot_ingress import preserve_contact_stops
        preserve_contact_stops(self.store,self.tenant,self.account)
        return {'outcome':'intake_only','projected':len(pending)}


def build_supervised_pilot(store, environ=None, *, model_transport=None, fans_transport=None):
    env = os.environ if environ is None else environ
    flag = env.get('TWIN_PLAN_C_SUPERVISED', '')
    if flag in ('', '0'):
        return None
    if (flag != '1' or store is None or env.get('ONLYFANS_PROCESS_ROLE') != 'worker'
            or not real_transport_enabled(env) or env.get('TWIN_PLAN_C_DRAFT_ONLY', '') not in ('', '0')):
        raise Refused('invalid supervised pilot startup')
    tenant, account = env.get('TWIN_PLAN_C_TENANT', ''), env.get('TWIN_PLAN_C_ACCOUNT', '')
    if not _NAME.fullmatch(tenant) or not _NAME.fullmatch(account):
        raise Refused('explicit pilot tenant and account are required')
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        scope, config = require(s, runtime)
        if scope.status != 'active':
            return IntakeOnlyPilot(store,tenant,account,_secret(env.get('TWIN_PLAN_C_WEBHOOK_SECRET_FILE','')))
        cfg = ModelConfig.from_env(env)
        require_model(config, account, cfg)
        policy = s.get(OfDeliveryPolicy, account)
        connection = s.scalar(select(OfFansConnection).where(OfFansConnection.account_id == account,
                                                              OfFansConnection.status == 'active'))
        if (not policy_matches(policy, config, enabled=True)
                or connection is None or connection.binding_id != runtime.binding_id or scope.active_since is None):
            raise Refused('supervised pilot startup prerequisites are not met')
        revision, fingerprint, fans = scope.revision, scope.fingerprint, frozenset(config['fans'])
        creator, epoch, provider_account = connection.provider_creator_id, connection.epoch, connection.provider_account_id
    profile_path = env.get('ENGINE_MODEL_PROFILES_FILE', '')
    profiles = JsonFileCreatorProfiles(profile_path)
    profile = profiles.get(account)
    journey = journey_config()
    if (profile is None or profile.missing_inputs()
            or (config.get('delivery_mode', 'full') != 'text_only' and not profile.offers_permitted)
            or profile.approved_item_ids != (str(config['product']['id']),)
            or journey.temporary_pause_reopen_hours is not None or journey.auto_backfill):
        raise Refused('supervised creator or journey configuration is incomplete')
    require_profile(config, profile)
    secret = _secret(env.get('TWIN_PLAN_C_WEBHOOK_SECRET_FILE', ''))
    provider = load_real_config(env, require_flag=True)
    if provider.provider_account != provider_account:
        raise Refused('provider account does not match the pilot connection')
    engine = build_engine_service(store, env, transport=model_transport)
    if engine is None:
        raise Refused('pilot model runtime is unavailable')
    engine.model.ledger.expected_pilot_scope = (revision, fingerprint)
    transport = build_real_transport(store, tenant, account, creator_id=creator, epoch=epoch, env=env,
                                     transport=fans_transport, allowed_chats=frozenset(map(str, fans)))
    worker = RealConversationWorker(engine, FansEventIntake(store), transport)
    pilot = SupervisedPilot(worker, tenant, account, secret, revision, fingerprint, fans, cfg, profiles)
    pilot.validate()
    return pilot
