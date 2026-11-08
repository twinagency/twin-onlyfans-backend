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

from sqlalchemy import and_, or_, select, tuple_
from sqlalchemy.orm import Session, aliased

from conversation_engine import approval
from conversation_engine.journey import journey_config
from conversation_engine.model_config import ModelConfig
from conversation_engine.pilot_scope import require, require_model, require_profile, policy_matches, _product_current
from conversation_engine.real_worker import RealConversationWorker
from conversation_engine.writer_context import JsonFileCreatorProfiles
from db.onlyfans_delivery_models import OfDeliveryPolicy
from db.onlyfans_engine_models import OfEngineApproval, OfEngineDecision, OfEngineModelCall, OfEnginePilotScope, OfEngineRequest, OfFansConnection, OfFansWebhook, OfEnginePilotWebhookDigest
from db.onlyfans_fans_intake_models import OfFansProjection
from db.onlyfans_models import OfIngress
from fans_api_adapter.events import FansEventIntake, _parse as parse_event
from fans_api_adapter.real_transport import build_real_transport, load_real_config, real_transport_enabled
from fans_api_adapter.webhooks import verify_signature
from onlyfans_runtime.store import Refused, utcnow
from .pilot_draft import _NAME, _secret
from .wiring import build_engine_service


def _event_fan(raw):
    try:
        body = parse_event(raw)
        event, payload = body['event'], body['payload']
        if event == 'messages.received': value = payload['fromUser']['id']
        elif event == 'messages.sent': value = payload['toUser']['id']
        elif event == 'messages.ppv.unlocked': value = payload['chat_id']
        elif event == 'subscriptions.new': value = payload['fan']['id']
        elif event == 'transactions.new': value = (payload.get('fan') or payload['user'])['id']
        elif event == 'chargebacks.new': value = payload['payment']['user']['id']
        elif event in {'accounts.connected','accounts.reconnected','accounts.disconnected',
                       'accounts.session_expired','accounts.authentication_failed'}:
            return event, None
        else:
            raise ValueError('unsupported event')
        if type(value) not in (int, str) or not str(value).isascii() or not str(value).isdecimal():
            raise ValueError('invalid fan')
        fan = int(value)
        if not 0 < fan < 2**63 or str(fan) != str(value):
            raise ValueError('invalid fan')
        return event, fan
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError):
        raise Refused('invalid pilot event') from None


def _record_outside_digest(store, tenant, account, raw, event_key, *, safety_hold=False):
    """Persist only a hash under the active epoch lock so a racing duplicate cannot be lost."""
    if type(event_key) is not str or not re.fullmatch(r'[\x21-\x7e]{1,255}', event_key):
        raise Refused('invalid webhook identity')
    digest = sha256(raw).hexdigest()
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        require(s, runtime)  # Retained intake evidence grants no model or send rights.
        active = s.scalar(select(OfFansConnection).where(OfFansConnection.account_id == account,
            OfFansConnection.binding_id == runtime.binding_id, OfFansConnection.status == 'active'))
        active_epoch = active.epoch if active is not None else None
        if active is None or active.binding_id != runtime.binding_id or active.status != 'active':
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
        marker = s.get(OfEnginePilotWebhookDigest, (account, epoch, event_key, digest))
        if marker is None:
            from sqlalchemy import func
            count = s.scalar(select(func.count()).select_from(OfEnginePilotWebhookDigest).where(
                OfEnginePilotWebhookDigest.account_id == account))
            if count >= 10000:
                raise Refused('pilot webhook evidence limit reached')
            s.add(OfEnginePilotWebhookDigest(account_id=account, epoch=epoch, event_key=event_key,
                                             payload_sha256=digest, safety_hold=(safety_hold or existing is not None and existing.payload_sha256 != digest), received_at=utcnow()))
        return existing is not None and existing.payload_sha256 != digest


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
                    or profile is None or profile.missing_inputs() or not profile.offers_permitted
                    or profile.approved_item_ids != (str(config['product']['id']),)):
                raise Refused('supervised pilot configuration changed')
            require_model(config, self.account, self.model_config)
            require_profile(config, profile)
            return scope.active_since

    def _record_outside_digest(self, raw, event_key, *, safety_hold=False):
        return _record_outside_digest(self.worker.engine.store, self.tenant, self.account,
                                      raw, event_key, safety_hold=safety_hold)

    def handle(self, raw, signature, event_key):
        if type(raw) is not bytes or len(raw) > 1_000_000 or not verify_signature(raw, signature, self.secret):
            raise Refused('webhook authentication failed')
        # Stopping planning/sending must not discard receipts, corrections or
        # financial observations. A stale worker may ingest only under the
        # current durable account/fan binding; it cannot plan or dispatch.
        with Session(self.worker.engine.store.engine) as s:
            runtime = self.worker.engine.store._runtime(s, self.tenant, self.account, lock=False)
            _, config = require(s, runtime)
            allowed_fans = frozenset(config['fans'])
        try:
            self.validate()
            planning = True
        except Refused:
            planning = False
        malformed = False
        try:
            _, fan = _event_fan(raw)
            outside = fan is not None and fan not in allowed_fans
        except Refused:
            outside, malformed = True, True
        if outside:
            if self._record_outside_digest(raw, event_key, safety_hold=malformed):
                return {'outcome':'quarantined', 'fenced':self.worker.engine.fence_conflicted_sources(self.tenant, self.account)}
            return {'outcome':'outside_pilot_scope'}
        if not planning:
            result = self.worker.intake.accept(raw, signature, self.secret, event_key,
                expected_tenant=self.tenant, expected_account=self.account)
            if result.get('outcome') == 'quarantined':
                self.worker.engine.fence_conflicted_sources(self.tenant, self.account)
            self.worker.engine.store.drain(self.tenant, self.account)
            from conversation_engine.pilot_ingress import preserve_contact_stops
            preserve_contact_stops(self.worker.engine.store, self.tenant, self.account)
            return {**result, 'planning':'held'}
        result = self.worker.handle(self.tenant, self.account, raw_body=raw, signature=signature,
                                    secret=self.secret, event_key=event_key)
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
            except Refused:
                # Authenticated but malformed input is held by the intake, never projected into another fan.
                intake._project(self.account, epoch, key)
        # Reclassify pending human-send observations before any approved dispatch.
        for fan in sorted(self.allowed_fans):
            self.validate()
            intake.drive(self.account, fan, fan_scoped=True)
            intake.classify_sent(self.account, fan)
        # Only scope-era inbound messages without a completed draft are eligible
        # for repair. Unknown model calls are retained, never automatically retried.
        with Session(store.engine) as s:
            runtime = store._runtime(s, self.tenant, self.account, lock=False)
            request = OfEngineRequest
            existing = select(OfEngineApproval.request_id).where(
                OfEngineApproval.account_id == request.account_id,
                OfEngineApproval.request_id == request.request_id).exists()
            ready = select(OfEngineDecision.decision_id).where(
                OfEngineDecision.account_id == request.account_id,
                OfEngineDecision.request_id == request.request_id,
                OfEngineDecision.binding_id == runtime.binding_id,
                OfEngineDecision.part_index == 0, OfEngineDecision.status == 'ready').exists()
            uncertain = select(OfEngineModelCall.call_id).where(
                OfEngineModelCall.account_id == request.account_id,
                OfEngineModelCall.request_id == request.request_id,
                or_(OfEngineModelCall.status.in_(('reserved','calling','unknown','released')),
                    OfEngineModelCall.outcome.in_(('identity_pending','identity_unverified')))).exists()
            query = (select(OfIngress, request).outerjoin(request, and_(
                request.account_id == OfIngress.account_id, request.fan_id == OfIngress.fan_id,
                request.source_message_id == OfIngress.message_id, request.binding_id == runtime.binding_id))
                .where(OfIngress.account_id == self.account, OfIngress.binding_id == runtime.binding_id,
                    OfIngress.fan_id.in_(self.allowed_fans), OfIngress.source != 'import',
                    OfIngress.status.in_(('pending','applied')), OfIngress.received_at >= active_since,
                    or_(request.request_id.is_(None), and_(request.status == 'open', ~existing,
                                                          or_(ready, ~uncertain)))))
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

    def handle(self, raw, signature, event_key):
        if type(raw) is not bytes or len(raw)>1_000_000 or not verify_signature(raw,signature,self.secret):
            raise Refused('webhook authentication failed')
        with Session(self.store.engine) as s:
            runtime=self.store._runtime(s,self.tenant,self.account,lock=False)
            _,config=require(s,runtime)
        malformed=False
        try:
            _,fan=_event_fan(raw)
            outside=fan is not None and fan not in config['fans']
        except Refused:
            outside,malformed=True,True
        if outside:
            conflict=_record_outside_digest(self.store,self.tenant,self.account,raw,event_key,safety_hold=malformed)
            return {'outcome':'quarantined' if conflict else 'outside_pilot_scope','planning':'held'}
        result=FansEventIntake(self.store).accept(raw,signature,self.secret,event_key,
            expected_tenant=self.tenant,expected_account=self.account)
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
    if (profile is None or profile.missing_inputs() or not profile.offers_permitted
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
