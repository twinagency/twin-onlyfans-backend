"""Authenticated Fans API webhook intake with a provider link and evidence projection.

Order of operations for every delivery:

1. verify the HMAC ``Signature`` over the exact raw bytes (forged -> nothing stored);
2. resolve the creator-scoped connection (the ``acct_`` id is mutable, the
   creator's ``onlyfans_id`` is the key) and dedupe on ``X-OFAPI-Idempotency-Key``;
3. store the RAW body first and commit - the provider is acknowledged only for
   what is durable;
4. project into canonical owners in a separate transaction. Webhooks are
   unordered and at-least-once, so projection is idempotent and re-drivable
   (``drive`` / ``reproject_pending``); a failed projection never loses evidence.

No network is used here and nothing is sent.
"""
from datetime import timedelta
from hashlib import sha256
import json
import re

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from db.models import Fan, Message, OfConversationState, OfCreatorBinding
from db.onlyfans_delivery_models import ENGINE_TRANSPORT_KINDS, OfDeliveryPolicy, OfTextIntent
from db.onlyfans_engine_models import OfEngineOwner, OfFansConnection, OfFansWebhook, OfFansWebhookConflict
from db.onlyfans_fans_intake_models import OfFansEvidence, OfFansLink, OfFansProjection
from onlyfans_runtime.store import Event, Refused, utcnow
from transaction_contract import strip_html
from . import payment_evidence as ev
from .intake import _message, _nonfinite, _unique_object
from .webhooks import verify_signature

ACCOUNT_EVENTS = {'accounts.connected', 'accounts.reconnected', 'accounts.session_expired',
                  'accounts.authentication_failed', 'accounts.disconnected',
                  'accounts.otp_code_required', 'accounts.face_otp_required'}
SUPPORTED = ACCOUNT_EVENTS | {'messages.received', 'messages.sent', 'messages.deleted', 'messages.ppv.unlocked',
                              'transactions.new', 'chargebacks.new', 'subscriptions.new',
                              'subscriptions.renewed', 'subscriptions.expired'}
# Only the offline lab transport may be driven by this intake until the live
# transport owner registers a Fans transport kind with the delivery policy.
ALLOWED_TRANSPORTS = ENGINE_TRANSPORT_KINDS
_KEY = re.compile(r'[\x21-\x7e]{1,255}\Z')


def _parse(raw):
    import decimal
    try:
        body = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object, parse_constant=_nonfinite,
                          parse_float=decimal.Decimal)
    except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise Refused('invalid webhook payload') from exc
    if type(body) is not dict or type(body.get('payload')) is not dict or type(body.get('account_id')) is not str:
        raise Refused('invalid webhook envelope')
    return body


def _pilot_fan_allowed(s, runtime, raw, event):
    from conversation_engine.pilot_scope import current
    from .event_scope import event_fan
    from sqlalchemy import literal
    pilot, config = current(s, runtime)
    if pilot is None:
        return True
    if event in ('subscriptions.renewed', 'subscriptions.expired'):
        # The schema-owned fan function predates these event types. Recheck the
        # actual fan both at intake and at projection; NULL must not widen scope.
        fan = ev.numeric(_parse(raw)['payload']['fan']['id'])
    else:
        fan = s.scalar(select(event_fan(literal(event), literal(raw))))
    return fan is None or int(fan) in config['fans']


class FansEventIntake:
    def __init__(self, store, *, human_grace_seconds=30, pair_window_seconds=1800, mismatch_settle_seconds=240):
        self.store, self.engine = store, store.engine
        self.grace = timedelta(seconds=human_grace_seconds)
        self.window = timedelta(seconds=pair_window_seconds)
        self.settle = timedelta(seconds=mismatch_settle_seconds)

    # ------------------------------------------------------------------ intake
    def accept(self, raw_body, signature, secret, event_key, *, expected_tenant=None, expected_account=None):
        if (expected_tenant is None) != (expected_account is None):
            raise Refused('complete expected scope required')
        if (type(raw_body) is not bytes or len(raw_body) > 1_000_000
                or not verify_signature(raw_body, signature, secret)
                or type(event_key) is not str or not _KEY.fullmatch(event_key)):
            raise Refused('webhook authentication or identity failed')
        body = _parse(raw_body)
        event = body.get('event')
        if event not in SUPPORTED:
            return {'outcome': 'ignored', 'event': str(event)[:80]}
        digest = sha256(raw_body).hexdigest()
        now = utcnow()
        with Session(self.engine) as s, s.begin():
            connection = self._resolve(s, body, event, now, expected_account=expected_account)
            if connection is None:
                raise Refused('provider account is not bound')
            # An authenticated provider event is not sufficient authority to
            # project it through a worker assigned to another creator/tenant.
            if expected_account is not None:
                from db.onlyfans_models import OfRuntime
                binding = s.get(OfCreatorBinding, connection.binding_id)
                runtime = self.store._runtime(s, expected_tenant, expected_account)
                if (connection.account_id != expected_account or binding is None or runtime is None
                        or binding.tenant_id != expected_tenant or connection.binding_id != runtime.binding_id):
                    raise Refused('webhook account scope mismatch')
                if not _pilot_fan_allowed(s, runtime, raw_body, event):
                    raise Refused('pilot fan scope changed before intake')
            account, epoch = connection.account_id, connection.epoch
            existing = s.get(OfFansWebhook, (account, epoch, event_key))
            from db.onlyfans_engine_models import OfEnginePilotWebhookDigest
            conflict = s.scalar(select(OfEnginePilotWebhookDigest.payload_sha256).where(
                OfEnginePilotWebhookDigest.account_id == account, OfEnginePilotWebhookDigest.epoch == epoch,
                OfEnginePilotWebhookDigest.event_key == event_key,
                OfEnginePilotWebhookDigest.payload_sha256 != digest).limit(1))
            if conflict is not None:
                if existing is None:
                    s.add(OfFansWebhook(account_id=account, epoch=epoch, event_key=event_key, event_type=event[:80],
                        payload_sha256=digest, raw_body=raw_body, status='quarantined', received_at=now))
                    s.flush()
                    s.add(OfFansProjection(account_id=account, epoch=epoch, event_key=event_key,
                        status='held', detail='pilot_event_key_conflict', attempts=0, updated_at=now))
                return {'outcome':'quarantined', 'account':account}
            if existing is not None:
                if existing.payload_sha256 != digest:
                    if s.get(OfFansWebhookConflict, (account, epoch, event_key, digest)) is None:
                        s.add(OfFansWebhookConflict(account_id=account, epoch=epoch, event_key=event_key,
                            payload_sha256=digest, raw_body=raw_body, received_at=now))
                    return {'outcome': 'quarantined', 'account': account}
                outcome = 'duplicate'
            else:
                s.add(OfFansWebhook(account_id=account, epoch=epoch, event_key=event_key, event_type=event[:80],
                    payload_sha256=digest, raw_body=raw_body, status='accepted', received_at=now))
                s.flush()
                s.add(OfFansProjection(account_id=account, epoch=epoch, event_key=event_key, status='pending',
                    attempts=0, updated_at=now))
                outcome = 'accepted'
        # Raw evidence is durable. Everything below is repairable projection.
        status = self._project(account, epoch, event_key)
        if status == 'projected' and event in ('accounts.otp_code_required', 'accounts.face_otp_required'):
            self._resolve_projected_challenge_digest(account, epoch, event_key)
        return {'outcome': outcome, 'account': account, 'epoch': epoch, 'projection': status}

    def _resolve_projected_challenge_digest(self, account, epoch, event_key):
        # A challenge first seen on an unbound provider account was retained as
        # a digest. Once the exact signed bytes arrive again through a verified
        # active binding and the stop has actually applied, classify only the
        # identical older digest. Never clear a conflict or another event.
        from conversation_engine.pilot_scope import current as current_pilot, digest_resolution_key
        from db.onlyfans_engine_models import OfEnginePilotWebhookDigest
        from db.onlyfans_models import OfRuntime
        from onlyfans_runtime.launch_operations_models import OfLaunchCommand
        with Session(self.engine) as s, s.begin():
            runtime = s.scalar(select(OfRuntime).where(OfRuntime.account_id == account).with_for_update())
            if runtime is None:
                return
            pilot, _ = current_pilot(s, runtime)
            if pilot is None:
                return
            connection = s.get(OfFansConnection, (account, epoch))
            raw = s.get(OfFansWebhook, (account, epoch, event_key))
            projection = s.get(OfFansProjection, (account, epoch, event_key))
            if (connection is None or connection.status != 'active' or connection.binding_id != runtime.binding_id or
                    raw is None or raw.status != 'accepted' or
                    raw.event_type not in ('accounts.otp_code_required', 'accounts.face_otp_required') or
                    projection is None or projection.status != 'projected' or
                    _parse(raw.raw_body).get('account_id') != connection.provider_account_id):
                return
            link = s.get(OfFansLink, (account, connection.provider_creator_id), with_for_update=True)
            policy = s.get(OfDeliveryPolicy, account)
            if (link is None or link.state != 'auth_failed' or not link.send_blocked or
                    link.block_reason != 'auth_challenge' or policy is None or policy.enabled is not False):
                return
            candidates = s.execute(select(OfEnginePilotWebhookDigest, OfFansConnection).join(OfFansConnection,
                and_(OfFansConnection.account_id == OfEnginePilotWebhookDigest.account_id,
                     OfFansConnection.epoch == OfEnginePilotWebhookDigest.epoch)).where(
                OfEnginePilotWebhookDigest.account_id == account,
                OfEnginePilotWebhookDigest.epoch != epoch,
                OfEnginePilotWebhookDigest.event_key == event_key,
                OfEnginePilotWebhookDigest.payload_sha256 == raw.payload_sha256,
                OfEnginePilotWebhookDigest.safety_hold.is_(True),
                OfFansConnection.binding_id == runtime.binding_id,
                OfFansConnection.provider_creator_id == connection.provider_creator_id)).all()
            for retained, old_connection in candidates:
                other = s.scalar(select(OfEnginePilotWebhookDigest.payload_sha256).where(
                    OfEnginePilotWebhookDigest.account_id == account,
                    OfEnginePilotWebhookDigest.epoch == retained.epoch,
                    OfEnginePilotWebhookDigest.event_key == event_key,
                    OfEnginePilotWebhookDigest.payload_sha256 != raw.payload_sha256).limit(1))
                old_raw = s.get(OfFansWebhook, (account, retained.epoch, event_key))
                conflict = s.scalar(select(OfFansWebhookConflict.payload_sha256).where(
                    OfFansWebhookConflict.account_id == account, OfFansWebhookConflict.epoch == retained.epoch,
                    OfFansWebhookConflict.event_key == event_key).limit(1))
                if other is not None or conflict is not None or (old_raw and old_raw.payload_sha256 != raw.payload_sha256):
                    continue
                key = digest_resolution_key(runtime.binding_id, retained.epoch, event_key, raw.payload_sha256)
                if s.get(OfLaunchCommand, (account, key)) is not None:
                    continue
                evidence = {'eventEpoch':retained.epoch, 'eventKey':event_key,
                            'payloadSha256':raw.payload_sha256, 'projectedEpoch':epoch}
                result = {'status':'exact_challenge_projected', 'sendBlocked':True}
                request_hash = sha256(json.dumps([account,evidence,result],sort_keys=True).encode()).hexdigest()
                s.add(OfLaunchCommand(account_id=account, command_key=key, binding_id=runtime.binding_id,
                    kind='plan_c_webhook_resolution', request_hash=request_hash, actor='fans-webhook',
                    reason='Exact signed provider challenge projected and sending blocked',
                    before_json=json.dumps(evidence,sort_keys=True), result_json=json.dumps(result,sort_keys=True),
                    created_at=utcnow()))

    def _resolve(self, s, body, event, now, *, expected_account=None):
        """Find the connection row for this delivery; a reconnect (new acct_) re-binds by creator id.

        Epochs are per Fastt account, so rows of different accounts are never ranked against each
        other: a scoped delivery loads only its own account's rows (another account's only existence-checked), and an unscoped one refuses a provider
        id that spans accounts rather than route it to whichever account has the higher epoch."""
        provider_id = body['account_id']
        q = select(OfFansConnection).where(OfFansConnection.provider_account_id == provider_id)
        if expected_account is not None:
            q = q.where(OfFansConnection.account_id == expected_account)
        matches = s.scalars(q.order_by(OfFansConnection.epoch.desc())).all()
        if expected_account is not None and not matches and s.scalar(select(OfFansConnection.account_id).where(
                OfFansConnection.provider_account_id == provider_id).limit(1)) is not None:
            raise Refused('webhook account scope mismatch')
        if len({c.account_id for c in matches}) > 1:
            raise Refused('provider account is bound to more than one account')
        connection = matches[0] if matches else None
        if connection is not None:
            active = s.scalar(select(OfFansConnection).where(OfFansConnection.account_id == connection.account_id,
                                                             OfFansConnection.status == 'active'))
            # A retired epoch of the SAME creator still carries valid late evidence; another creator's does not.
            if connection.status != 'active' and active is not None and (
                    active.provider_creator_id != connection.provider_creator_id):
                raise Refused('provider binding changed')
            return connection
        if event not in {'accounts.connected', 'accounts.reconnected'}:
            return None
        creator = body['payload'].get('onlyfans_id')
        if type(creator) not in (str, int) or not str(creator).strip():
            return None
        creator = str(creator)
        prior = s.scalars(select(OfFansConnection).where(OfFansConnection.provider_creator_id == creator)
                          .order_by(OfFansConnection.epoch.desc())).all()
        accounts = {c.account_id for c in prior}
        if len(accounts) != 1 or (expected_account is not None and accounts != {expected_account}):
            return None  # unknown, ambiguous or another account's creator: never invent a binding or lock it
        account = accounts.pop()
        latest = prior[0]
        binding = s.get(OfCreatorBinding, latest.binding_id)
        runtime = self.store._runtime(s, binding.tenant_id, account)
        from conversation_engine.pilot_scope import current as current_pilot
        pilot, _ = current_pilot(s, runtime)
        existing_link = s.get(OfFansLink, (account, creator), with_for_update=True)
        if pilot is not None or (existing_link is not None and existing_link.block_reason == 'auth_challenge'):
            # A signed event alone does not prove a changed provider identity.
            # The pilot records a held receipt and requires current provider proof.
            raise Refused('authentication challenge recovery is unverified')
        policy = s.get(OfDeliveryPolicy, account)
        if policy is None or policy.transport_kind not in ALLOWED_TRANSPORTS:
            raise Refused('webhook intake is limited to the synthetic lab')
        for row in s.scalars(select(OfFansConnection).where(OfFansConnection.account_id == account,
                OfFansConnection.status == 'active').with_for_update()):
            row.status = 'retired'
        s.flush()
        epochs = s.scalars(select(OfFansConnection.epoch).where(OfFansConnection.account_id == account)).all()
        fresh = OfFansConnection(account_id=account, epoch=max(epochs) + 1, binding_id=runtime.binding_id,
            provider_account_id=provider_id, provider_creator_id=creator, status='active', created_at=now)
        s.add(fresh)
        s.flush()
        link = self._link(s, fresh, now)
        link.reconnect_count += 1
        return fresh

    @staticmethod
    def _link(s, connection, now):
        link = s.get(OfFansLink, (connection.account_id, connection.provider_creator_id), with_for_update=True)
        if link is None:
            link = OfFansLink(account_id=connection.account_id, creator_id=connection.provider_creator_id,
                provider_account_id=connection.provider_account_id, state='connected', send_blocked=False,
                session_refreshes=0, reconnect_count=0, updated_at=now)
            s.add(link)
            s.flush()
        return link

    # -------------------------------------------------------------- projection
    def _project(self, account, epoch, event_key):
        """Project one stored delivery. Safe to call repeatedly; returns the projection status."""
        now = utcnow()
        after = []
        with Session(self.engine) as s, s.begin():
            from db.onlyfans_models import OfRuntime
            s.scalar(select(OfRuntime).where(OfRuntime.account_id == account).with_for_update())
            row = s.get(OfFansProjection, (account, epoch, event_key), with_for_update=True)
            if row is None or row.status != 'pending':
                return row.status if row is not None else 'missing'
            raw = s.get(OfFansWebhook, (account, epoch, event_key))
            connection = s.get(OfFansConnection, (account, epoch))
            from db.onlyfans_models import OfRuntime
            runtime = s.get(OfRuntime, account)
            if connection is None or runtime is None or connection.binding_id != runtime.binding_id:
                # Retain raw and projection; never project an old creator's
                # event through the currently bound creator's account.
                row.status, row.detail, row.updated_at = 'held', 'historical_creator_binding', now
                return row.status
            if not _pilot_fan_allowed(s, runtime, raw.raw_body, raw.event_type):
                row.status, row.detail, row.updated_at = 'held', 'pilot_fan_scope_changed', now
                return row.status
            body = _parse(raw.raw_body)
            link = self._link(s, connection, now)
            row.attempts += 1
            try:
                with s.begin_nested():
                    status, detail = self._dispatch(s, body, raw, connection, link, now, after)
            except (Refused, ValueError, TypeError, KeyError, AttributeError) as exc:
                status, detail, after = 'held', ('invalid:' + str(exc))[:96], []
            row.status, row.detail, row.updated_at = status, detail, now
            if status != 'pending':
                link.last_event_at = max(link.last_event_at or now, now)
                link.last_event_type = raw.event_type
                link.provider_account_id = self._current_account(s, connection, link)
        for action in after:
            action()
        return status

    @staticmethod
    def _current_account(s, connection, link):
        active = s.scalar(select(OfFansConnection).where(OfFansConnection.account_id == connection.account_id,
            OfFansConnection.status == 'active', OfFansConnection.provider_creator_id == link.creator_id))
        return active.provider_account_id if active is not None else link.provider_account_id

    def _dispatch(self, s, body, raw, connection, link, now, after):
        event, payload, account = raw.event_type, body['payload'], connection.account_id
        policy = s.get(OfDeliveryPolicy, account)
        if policy is None or policy.transport_kind not in ALLOWED_TRANSPORTS:
            raise Refused('webhook intake is limited to the synthetic lab')
        current = self._current_account(s, connection, link)
        if event in ACCOUNT_EVENTS:
            return self._account(s, event, payload, body['account_id'], current, connection, link, now, after)
        # Data events for a replaced acct_ id are still this creator's evidence (checked in _resolve).
        if event == 'messages.received':
            binding = s.get(OfCreatorBinding, connection.binding_id)
            runtime = self.store._runtime(s, binding.tenant_id, account)
            message = _message({'event': event, 'payload': payload})
            result = self.store._record(s, runtime, message, message.payload(), 'ws', now)
            return ('pending', 'inbox_backpressure') if result == 'backpressure' else ('projected', result)
        if event == 'messages.sent':
            return self._sent(s, payload, raw, connection, now, after)
        if event == 'messages.deleted':
            return self._deleted(s, payload, raw, connection, current)
        if event == 'subscriptions.new':
            return self._subscription(s, payload, connection)
        if event in ('subscriptions.renewed', 'subscriptions.expired'):
            return self._subscription_lifecycle(event, payload, link)
        if event == 'messages.ppv.unlocked':
            return self._unlock(s, payload, raw, connection, now, after)
        if event == 'transactions.new':
            return self._transaction(s, payload, raw, connection, now, after, source='webhook')
        if event == 'chargebacks.new':
            return self._chargeback(s, payload, raw.event_key, ev.digest(json.loads(raw.raw_body)), connection, now, after, 'webhook')
        raise Refused('unsupported event')

    def _deleted(self, s, payload, raw, connection, current):
        # Only a provider account and message ID are supplied. Ambiguity,
        # retired accounts and missing source evidence remain held. A proven
        # outside-pilot message is classified only after exact epoch mapping.
        from conversation_engine.history_scope import current_binding_message
        from conversation_engine.pilot_scope import current as current_pilot
        from db.onlyfans_models import OfRuntime
        from db.onlyfans_engine_models import OfEngineDecision
        from sqlalchemy import func
        from sqlalchemy.dialects.postgresql import JSONB
        runtime = s.get(OfRuntime, connection.account_id)
        if connection.status != 'active' or connection.provider_account_id != current:
            return 'held', 'deletion_historical_connection'
        message_id = ev.numeric(payload.get('id'))
        rows = s.scalars(select(Message).where(Message.account_id == connection.account_id,
            Message.message_id == message_id).limit(2)).all()
        if len(rows) != 1:
            return 'held', 'deletion_mapping_missing_or_ambiguous'
        message = rows[0]
        if not s.scalar(select(current_binding_message(runtime.binding_id)).where(
                Message.account_id == message.account_id, Message.fan_id == message.fan_id,
                Message.message_id == message.message_id)):
            return 'held', 'deletion_binding_unproven'
        pilot, config = current_pilot(s, runtime)
        body = func.convert_from(OfFansWebhook.raw_body, 'UTF8').cast(JSONB)
        incoming = s.scalar(select(OfFansWebhook.event_key).join(OfFansProjection,
            (OfFansProjection.account_id == OfFansWebhook.account_id) &
            (OfFansProjection.epoch == OfFansWebhook.epoch) &
            (OfFansProjection.event_key == OfFansWebhook.event_key)).where(
            OfFansWebhook.account_id == connection.account_id, OfFansWebhook.epoch == connection.epoch,
            OfFansWebhook.event_type == 'messages.received', OfFansProjection.status == 'projected',
            func.jsonb_extract_path_text(body, 'payload', 'id') == str(message_id),
            func.jsonb_extract_path_text(body, 'payload', 'fromUser', 'id') == str(message.fan_id)).limit(1))
        outgoing = s.scalar(select(OfTextIntent.intent_id).join(OfEngineDecision,
            (OfEngineDecision.account_id == OfTextIntent.account_id) &
            (OfEngineDecision.intent_id == OfTextIntent.intent_id)).where(
            OfTextIntent.account_id == connection.account_id, OfTextIntent.fan_id == message.fan_id,
            OfTextIntent.provider_message_id == message_id, OfTextIntent.binding_id == runtime.binding_id,
            OfTextIntent.status == 'accepted', OfTextIntent.projected_at.is_not(None),
            OfEngineDecision.connection_epoch == connection.epoch).limit(1))
        if outgoing is None and message.direction == 'out':
            outgoing = self._outside_writer_send(s, connection, message)
        if (message.direction not in ('in', 'out') or
                (message.direction == 'in' and incoming is None) or
                (message.direction == 'out' and outgoing is None)):
            return 'held', 'deletion_epoch_unproven'
        if pilot is not None and message.fan_id not in config['fans']:
            # Proven unrelated to both pilot fans. Retain the webhook and its
            # projection, without modifying the other fan or holding the pilot.
            return 'ignored', 'deletion_outside_pilot'
        # Only the display flag changes. Immutable ingress, contact control,
        # accepted intents, purchase/access evidence and financial history remain.
        message.is_unsent = True
        return 'projected', f'deleted:{message.fan_id}:{message_id}'

    @staticmethod
    def _outside_writer_send(s, connection, message):
        """Epoch proof for an outgoing message the engine did not send.

        Only a retained signed messages.sent receipt of THIS connection epoch for exactly this recipient and
        message id, already accepted as takeover evidence (applied human_sent), proves it. It is evidence about an
        outside writer, never an engine intent, so no accepted-intent or send authority is created.
        """
        evidence = s.scalar(select(OfFansEvidence).where(OfFansEvidence.account_id == connection.account_id,
            OfFansEvidence.fan_id == message.fan_id, OfFansEvidence.message_id == message.message_id,
            OfFansEvidence.kind == 'human_sent', OfFansEvidence.status == 'applied').limit(1))
        if evidence is None or not evidence.event_key:
            return None
        receipt = s.get(OfFansWebhook, (connection.account_id, connection.epoch, evidence.event_key))
        projection = s.get(OfFansProjection, (connection.account_id, connection.epoch, evidence.event_key))
        if (receipt is None or projection is None or projection.status != 'projected' or receipt.status != 'accepted'
                or receipt.event_type != 'messages.sent' or receipt.payload_sha256 != evidence.raw_sha256):
            return None
        try:
            sent = _parse(bytes(receipt.raw_body))['payload']
            if (ev.numeric(sent.get('id')) != message.message_id or ev.numeric(sent['toUser'].get('id')) != message.fan_id
                    or sent.get('isFromQueue') is True):
                return None
        except (Refused, ValueError, TypeError, KeyError, AttributeError):
            return None
        return evidence.evidence_id

    # ---------------------------------------------------------------- accounts
    def _account(self, s, event, payload, provider_id, current, connection, link, now, after):
        if provider_id != current and event != 'accounts.reconnected' and event != 'accounts.connected':
            return 'ignored', 'superseded_account'  # a late event about a replaced acct_ id
        creator = payload.get('onlyfans_id')
        if creator is not None and str(creator) != link.creator_id:
            raise Refused('account event names another creator')
        if event in ('accounts.connected', 'accounts.reconnected'):
            if link.block_reason == 'auth_challenge':
                # A signed success may be a delayed pre-challenge event; it is
                # not proof of current provider authentication.
                return 'held', 'challenge_recovery_unverified'
            if provider_id == current:
                link.provider_account_id = provider_id
                if link.state != 'connected':
                    link.state = 'connected'
            return 'projected', 'connected'
        if event == 'accounts.session_expired':
            link.session_refreshes += 1  # the provider re-connects it itself: informational only
            return 'projected', 'session_refreshing'
        challenge = event in ('accounts.otp_code_required', 'accounts.face_otp_required')
        link.state = 'auth_failed' if event == 'accounts.authentication_failed' or challenge else 'disconnected'
        challenge_hold = challenge or link.block_reason == 'auth_challenge'
        link.send_blocked, link.block_reason, link.updated_at = True, 'auth_challenge' if challenge_hold else link.state, now
        if event == 'accounts.disconnected':
            connection.status = 'retired'  # that acct_ id answers 404 from now on
        tenant, account, reason = s.get(OfCreatorBinding, connection.binding_id).tenant_id, connection.account_id, link.state
        after.append(lambda: self._disable_sends(tenant, account, reason))
        return 'projected', reason

    def _disable_sends(self, tenant, account, reason):
        from onlyfans_runtime.text_delivery import TextAuthority
        authority = TextAuthority(self.store)
        for _ in range(3):
            with Session(self.engine) as s:
                policy = s.get(OfDeliveryPolicy, account)
                if policy is None or not policy.enabled:
                    return
                revision, limit, throttle = policy.revision, policy.limit_units, policy.throttle_seconds
            try:
                authority.configure(tenant, account, expected_revision=revision, enabled=False, limit_units=limit,
                    throttle_seconds=throttle, actor='fans-webhook', reason=('Provider connection: ' + reason)[:512])
                return
            except Refused as exc:
                if 'revision' not in str(exc):
                    raise

    def release_send_block(self, tenant, account, creator_id, *, actor, reason):
        """Operator re-enables sending after a reconnect. Never automatic."""
        from onlyfans_runtime.text_delivery import TextAuthority
        with Session(self.engine) as s, s.begin():
            runtime = self.store._runtime(s, tenant, account)
            from conversation_engine.pilot_scope import current as current_pilot, authorize
            pilot, config = current_pilot(s, runtime)
            if pilot is not None:
                authorize(config, actor, 'activate')
            link = s.get(OfFansLink, (account, str(creator_id)), with_for_update=True)
            if link is None or link.state != 'connected':
                raise Refused('provider connection is not healthy')
            link.send_blocked, link.block_reason, link.updated_at = False, None, utcnow()
            policy = s.get(OfDeliveryPolicy, account)
            revision, limit, throttle = policy.revision, policy.limit_units, policy.throttle_seconds
        TextAuthority(self.store).configure(tenant, account, expected_revision=revision, enabled=True,
            limit_units=limit, throttle_seconds=throttle, actor=actor, reason=reason)

    # --------------------------------------------------------------- evidence
    def _drive_after(self, account, fan_id, after):
        after.append(lambda: self.drive(account, fan_id))

    def _subscription(self, s, payload, connection):
        """A new subscriber. In this event payload.user_id is the CREATOR and payload.fan.id is the fan.

        Nothing is created here: the durable projection row carries the fan id
        (`subscription_new:<fan>`) so the journey layer can start a welcome once, later.
        """
        link = s.get(OfFansLink, (connection.account_id, connection.provider_creator_id))
        if str(payload.get('user_id')) != link.creator_id:
            raise Refused('subscription does not belong to this creator')
        fan = payload.get('fan')
        if type(fan) is not dict:
            raise Refused('subscription fan is missing')
        fan_id = ev.numeric(fan.get('id'))
        if str(fan_id) == link.creator_id:
            raise Refused('subscription names the creator as the fan')
        return 'projected', f'subscription_new:{fan_id}'

    @staticmethod
    def _subscription_lifecycle(event, payload, link):
        fan = payload.get('fan')
        if type(fan) is not dict:
            raise Refused('subscription fan is missing')
        fan_id = ev.numeric(fan.get('id'))
        if str(fan_id) == link.creator_id:
            raise Refused('subscription names the creator as the fan')
        user = payload.get('user')
        if user is not None and (type(user) is not dict or ev.numeric(user.get('id')) != fan_id):
            raise Refused('subscription fan identity conflicts')
        expected_user = link.creator_id if event == 'subscriptions.renewed' else str(fan_id)
        if str(payload.get('user_id')) != expected_user:
            raise Refused('subscription identity conflicts')
        # Validate provider timestamps, retain the immutable original receipt,
        # and never turn subscription lifecycle into photo purchase evidence.
        ev.stamp(payload.get('createdAt'))
        if event == 'subscriptions.expired':
            ev.stamp(payload.get('expiredAt'))
        return 'projected', f"{event.replace('.', '_')}:{fan_id}"

    def _unlock(self, s, payload, raw, connection, now, after):
        link = s.get(OfFansLink, (connection.account_id, connection.provider_creator_id))
        # In this event user_id is the CREATOR; the fan is chat_id (and payload.fan).
        if str(payload.get('user_id')) != link.creator_id:
            raise Refused('unlock does not belong to this creator')
        fan_id = ev.numeric(payload.get('chat_id'))
        fan = payload.get('fan')
        if type(fan) is dict and ev.numeric(fan.get('id')) != fan_id:
            raise Refused('unlock fan identity conflicts')
        if str(fan_id) == link.creator_id:
            raise Refused('unlock names the creator as the fan')
        message_id = ev.numeric(payload.get('message_id'))
        _, outcome = ev.add(s, connection.account_id, dedupe=f'unlock:{message_id}', kind='unlock', source='webhook',
            event_key=raw.event_key, fan_id=fan_id, message_id=message_id, amount=ev.cents(payload.get('amountGross')),
            occurred=ev.stamp(payload.get('createdAt')), raw_sha256=raw.payload_sha256, now=now)
        self._drive_after(connection.account_id, fan_id, after)
        return ('held', 'conflicting_duplicate') if outcome == 'conflict' else ('projected', 'unlock_' + outcome)

    def _transaction(self, s, payload, raw, connection, now, after, *, source):
        account = connection.account_id
        kind = payload.get('type')
        status = payload.get('status')
        if kind not in ('message', 'chat_messages'):
            return 'ignored', 'not_a_message_payment'
        fan = payload.get('fan') if type(payload.get('fan')) is dict else payload.get('user')
        if type(fan) is not dict:
            raise Refused('transaction fan is missing')
        fan_id = ev.numeric(fan.get('id'))
        txid = ev.transaction_id(payload.get('id'))
        amount = ev.cents(payload.get('amount'))
        if payload.get('currency') not in (None, 'USD'):
            raise Refused('unsupported currency')
        occurred = ev.stamp(payload.get('created_at') or payload.get('createdAt'))
        event_key = raw.event_key if raw is not None else None
        digest = raw.payload_sha256 if raw is not None else ev.digest(payload)
        _, outcome = ev.add(s, account, dedupe=f'tx:{txid}', kind='transaction', source=source, event_key=event_key,
            fan_id=fan_id, txid=txid, amount=amount, provider_status=str(status)[:24] if status is not None else None,
            occurred=occurred, raw_sha256=digest, now=now)
        if status == 'undo':  # the provider's word for a reversed payment
            self._chargeback(s, {'payment': {'id': txid, 'amount': payload.get('amount'), 'user': fan,
                'createdAt': payload.get('created_at') or payload.get('createdAt')}}, event_key, digest,
                connection, now, after, source)
        self._drive_after(account, fan_id, after)
        return ('held', 'conflicting_duplicate') if outcome == 'conflict' else ('projected', 'transaction_' + outcome)

    def _chargeback(self, s, payload, event_key, digest, connection, now, after, source):
        # Documented list-chargebacks row shape: payment.{id,amount,user.id,createdAt}.
        payment = payload.get('payment')
        if type(payment) is not dict or type(payment.get('user')) is not dict:
            raise Refused('chargeback payment is missing')
        txid = ev.transaction_id(payment.get('id'))
        fan_id = ev.numeric(payment['user'].get('id'))
        _, outcome = ev.add(s, connection.account_id, dedupe=f'cb:{txid}', kind='chargeback', source=source,
            event_key=event_key, fan_id=fan_id, txid=txid, amount=ev.cents(payment.get('amount')),
            occurred=ev.stamp(payload.get('createdAt') or payment.get('createdAt')), raw_sha256=digest, now=now)
        self._drive_after(connection.account_id, fan_id, after)
        return ('held', 'conflicting_duplicate') if outcome == 'conflict' else ('projected', 'chargeback_' + outcome)

    def _sent(self, s, payload, raw, connection, now, after):
        """A message the creator account sent. Ours are known by provider id; any other is a human reply."""
        to_user = payload.get('toUser')
        if type(to_user) is not dict:
            raise Refused('sent message recipient is missing')
        fan_id = ev.numeric(to_user.get('id'))
        message_id = ev.numeric(payload.get('id'))
        if payload.get('isFromQueue') is True:
            # A scheduled/mass send is not a reply in this chat, so it is not takeover evidence. But a queued send that
            # reached a fan the engine owns is a competing writer the local fence cannot stop (it was queued at the
            # provider). It must stop THAT fan's sends: a held human_sent evidence row is read by delivery_block on
            # the send path (and by planning), and only an audited operator resolution releases it. It is not
            # classified as takeover and never touches another fan.
            from conversation_engine.pilot_scope import current as current_pilot
            from db.onlyfans_models import OfRuntime
            owner = s.get(OfEngineOwner, (connection.account_id, fan_id))
            pilot, pilot_config = current_pilot(s, s.get(OfRuntime, connection.account_id))
            owned = (owner is not None and owner.mode == 'active') or (
                pilot is not None and fan_id in pilot_config['fans'])
            if owned:
                row, outcome = ev.add(s, connection.account_id, dedupe=f'sent:{message_id}', kind='human_sent',
                    source='webhook', event_key=raw.event_key, fan_id=fan_id, message_id=message_id,
                    occurred=ev.stamp(payload.get('createdAt')), raw_sha256=raw.payload_sha256, now=now)
                if outcome == 'conflict':
                    return 'held', 'conflicting_duplicate'
                if outcome == 'new':
                    row.status, row.reason = 'held', 'queued_send_to_engine_owned_fan'
                ev.attention(s, connection.account_id, fan_id)
                return 'projected', 'queued_send_to_engine_owned_fan'
            return 'ignored', 'queued_broadcast'
        account = connection.account_id
        row, outcome = ev.add(s, connection.account_id, dedupe=f'sent:{message_id}', kind='human_sent',
            source='webhook', event_key=raw.event_key, fan_id=fan_id, message_id=message_id,
            occurred=ev.stamp(payload.get('createdAt')), raw_sha256=raw.payload_sha256, now=now)
        if outcome == 'conflict':
            return 'held', 'conflicting_duplicate'
        row.reason = 'awaiting_classification' if outcome == 'new' else row.reason
        after.append(lambda: self.classify_sent(account, fan_id))
        return 'projected', 'sent_' + outcome

    # ------------------------------------------------------------ re-drivable
    def drive(self, account, fan_id, *, fan_scoped=False):
        """Link unlocks to their sends, settle them, and flag unlinked reversals. Order-independent and idempotent.

        Transactions and chargebacks are never paired with an unlock by amount or time (see payment_evidence).
        """
        for _ in range(4):
            now = utcnow()
            with Session(self.engine) as s, s.begin():
                link = s.scalar(select(OfFansLink).where(OfFansLink.account_id == account).with_for_update())
                if link is None:
                    return
                changed = ev.match(s, account, fan_id, now)
                ready = ev.linked_unlocks(s, account, fan_id if fan_scoped else None)
                charged = ev.pending_chargebacks(s, account, fan_id if fan_scoped else None)
            progressed = changed
            for unlock_id in ready:
                result = ev.settle_unlock(self.store, account, unlock_id, now)
                progressed = progressed or result not in ('pending', 'deferred')
            for chargeback_id in charged:
                ev.flag_unlinked_reversal(self.store, account, chargeback_id, now)
            if not progressed:
                return

    def classify_sent(self, account, fan_id):
        """Turn unexplained creator-account sends into takeover evidence, once safely past our own sends."""
        now = utcnow()
        takeovers = []
        with Session(self.engine) as s, s.begin():
            rows = s.scalars(select(OfFansEvidence).where(OfFansEvidence.account_id == account,
                OfFansEvidence.fan_id == fan_id, OfFansEvidence.kind == 'human_sent',
                OfFansEvidence.status == 'pending').order_by(OfFansEvidence.occurred_at).with_for_update()).all()
            from db.onlyfans_models import OfRuntime
            from .evidence_scope import webhook_binding
            runtime = s.get(OfRuntime, account)
            policy = s.get(OfDeliveryPolicy, account)
            for row in rows:
                if policy is not None and policy.transport_kind == 'fansapi' and (
                        runtime is None or webhook_binding(s, row) != runtime.binding_id):
                    row.reason, row.updated_at = 'creator_scope_unverified', now
                    continue
                own = s.scalar(select(OfTextIntent.intent_id).where(OfTextIntent.account_id == account,
                    OfTextIntent.binding_id == runtime.binding_id, OfTextIntent.provider_message_id == row.message_id))
                if own is not None:
                    row.status, row.reason, row.intent_id, row.updated_at = 'dismissed', 'own_send', own, now
                    continue
                unresolved = s.scalar(select(OfTextIntent.intent_id).where(OfTextIntent.account_id == account,
                    OfTextIntent.binding_id == runtime.binding_id, OfTextIntent.fan_id == fan_id,
                    OfTextIntent.status.in_(('dispatching', 'unknown'))).limit(1))
                if unresolved is not None:
                    row.reason, row.updated_at = 'send_unresolved', now
                elif now - row.created_at < self.grace:
                    row.reason, row.updated_at = 'within_grace', now
                else:
                    takeovers.append(row.evidence_id)
        for evidence_id in takeovers:
            self._takeover(account, fan_id, evidence_id, now)

    def _takeover(self, account, fan_id, evidence_id, now):
        from db.onlyfans_models import OfRuntime
        with Session(self.engine) as s, s.begin():
            row = s.get(OfFansEvidence, (account, evidence_id), with_for_update=True)
            if row.status != 'pending':
                return
            runtime = s.get(OfRuntime, account)
            if runtime is None:
                return
            policy = s.get(OfDeliveryPolicy, account)
            from .evidence_scope import webhook_binding
            if policy is not None and policy.transport_kind == 'fansapi' and webhook_binding(s, row) != runtime.binding_id:
                row.reason, row.updated_at = 'creator_scope_unverified', now
                return
            binding = s.get(OfCreatorBinding, runtime.binding_id)
            if binding is None:
                return
            raw = s.scalar(select(OfFansWebhook.raw_body).join(OfFansConnection, and_(
                OfFansConnection.account_id == OfFansWebhook.account_id,
                OfFansConnection.epoch == OfFansWebhook.epoch)).where(OfFansWebhook.account_id == account,
                OfFansConnection.binding_id == runtime.binding_id, OfFansWebhook.event_key == row.event_key,
                OfFansWebhook.payload_sha256 == row.raw_sha256).order_by(OfFansWebhook.epoch.desc()).limit(1)) if row.event_key else None
            if raw is not None:
                payload = _parse(raw)['payload']
                text = strip_html(payload.get('text') or '')
                media = tuple(ev.numeric(m.get('id')) for m in payload.get('media') or () if type(m) is dict and 'id' in m)
                try:
                    message = Event(fan_id, row.message_id, payload['createdAt'], 'out', text, media, 0).normalized()
                except (Refused, KeyError):
                    message = None  # history stays repairable; the takeover evidence itself stands
                if message is not None:
                    self.store._record(s, self.store._runtime(s, binding.tenant_id, account), message,
                                       message.payload(), 'ws', now)
            state = s.get(OfConversationState, (account, fan_id))
            tenant = binding.tenant_id
            binding_id = runtime.binding_id
            revision = state.revision if state is not None else 0
            already = state is not None and (state.mode == 'human_controlled' or state.takeover_state != 'none')
        with Session(self.engine) as s:
            from conversation_engine.pilot_scope import trial_takeover_exception
            excepted = trial_takeover_exception(s, account, binding_id, fan_id)
        if excepted and not already:
            # Owner-recorded temporary trial exception: evidence and history above are kept; no takeover request.
            result = 'outside_send_recorded_trial_exception'
        elif already:
            result = 'already_human'
        else:
            try:
                self.store.control(tenant, account, fan_id, mode='human_controlled', expected_revision=revision,
                    command_key='fans-takeover:' + sha256(f'{account}:{fan_id}:{evidence_id}'.encode()).hexdigest()[:40],
                    actor='fans-webhook', reason='Creator account sent a message this engine did not send.',
                    action='request_takeover')
                result = 'takeover_requested'
            except Refused as exc:
                with Session(self.engine) as s, s.begin():
                    row = s.get(OfFansEvidence, (account, evidence_id))
                    row.reason, row.updated_at = ('takeover_deferred:' + str(exc))[:96], now
                return
        with Session(self.engine) as s, s.begin():
            row = s.get(OfFansEvidence, (account, evidence_id))
            row.status, row.reason, row.updated_at = 'applied', result, now

    def reproject_pending(self, account):
        """Re-drive deliveries whose projection did not finish (crash, backpressure, retried webhook)."""
        with Session(self.engine) as s:
            keys = s.execute(select(OfFansProjection.epoch, OfFansProjection.event_key).where(
                OfFansProjection.account_id == account, OfFansProjection.status == 'pending')
                .order_by(OfFansProjection.updated_at)).all()
        return {key: self._project(account, epoch, key) for epoch, key in keys}

    def sweep(self, account):
        """Periodic re-evaluation of pending evidence (late sends, grace expiry, settle windows)."""
        with Session(self.engine) as s:
            fans = {r[0] for r in s.execute(select(OfFansEvidence.fan_id).where(OfFansEvidence.account_id == account,
                OfFansEvidence.status == 'pending'))}
        for fan_id in sorted(fans):
            self.drive(account, fan_id)
            self.classify_sent(account, fan_id)

    def resolve_held(self, account, evidence_id, *, actor, reason):
        """A person dismisses a held row. It is never converted to a grant here."""
        with Session(self.engine) as s, s.begin():
            row = s.get(OfFansEvidence, (account, evidence_id), with_for_update=True)
            if row is None or row.status != 'held' or not 1 <= len(actor) <= 128 or not 1 <= len(reason) <= 96:
                raise Refused('no held evidence to resolve')
            row.status, row.resolved_by, row.reason, row.updated_at = 'dismissed', actor, reason, utcnow()

    # ------------------------------------------------- backfill entry points
    def ingest_transaction_row(self, account, row, *, source='backfill'):
        """A documented list-transactions row, handled exactly like transactions.new."""
        with Session(self.engine) as s, s.begin():
            connection = s.scalar(select(OfFansConnection).where(OfFansConnection.account_id == account,
                OfFansConnection.status == 'active'))
            if connection is None:
                raise Refused('no active provider connection')
            link = self._link(s, connection, utcnow())
            after = []
            payload = dict(row)
            payload.setdefault('created_at', row.get('createdAt'))
            if 'fan' not in payload:
                payload['fan'] = row.get('user')
            result = self._transaction(s, payload, None, connection, utcnow(), after, source=source)
        for action in after:
            action()
        return result

    def ingest_chargeback_row(self, account, row, *, source='backfill'):
        with Session(self.engine) as s, s.begin():
            connection = s.scalar(select(OfFansConnection).where(OfFansConnection.account_id == account,
                OfFansConnection.status == 'active'))
            if connection is None:
                raise Refused('no active provider connection')
            after = []
            result = self._chargeback(s, row, None, ev.digest(row), connection, utcnow(), after, source)
        for action in after:
            action()
        return result

    def ingest_history_item(self, tenant, account, chat_id, item, *, source='backfill'):
        """One history message: canonical text/media context plus separate PPV unlock evidence.
        Old creator replies are deliberately NOT takeover evidence."""
        fan_id = ev.numeric(chat_id)
        sender = item.get('fromUser') if type(item.get('fromUser')) is dict else {}
        now = utcnow()
        if sender.get('id') is not None and ev.numeric(sender['id']) == fan_id and item.get('isSentByMe') is not True:
            message = _message({'event': 'messages.received', 'payload': dict(item, fromUser=sender)})
            return self.store.record(tenant, account, message, source='catchup')
        message_id = ev.numeric(item.get('id'))
        after = []
        with Session(self.engine) as s, s.begin():
            runtime = self.store._runtime(s, tenant, account, lock=True)
            connection = s.scalar(select(OfFansConnection).where(OfFansConnection.account_id == account,
                OfFansConnection.binding_id == runtime.binding_id, OfFansConnection.status == 'active'))
            if connection is None:
                raise Refused('no active provider connection')
            sender_id = ev.numeric(sender['id']) if sender.get('id') is not None else None
            if (item.get('isSentByMe') is False
                    or sender_id is not None and str(sender_id) != connection.provider_creator_id
                    or sender_id is None and item.get('isSentByMe') is not True):
                raise Refused('outgoing history creator or direction is unverified')
            text = item.get('text')
            if text is None:
                text = ''
            media = item.get('media', [])
            if type(text) is not str or type(media) is not list or any(type(m) is not dict for m in media):
                raise Refused('outgoing history text or media is malformed')
            price = item.get('price')
            if type(price) is bool:
                raise Refused('outgoing history price is malformed')
            price_cents = 0 if price in (None, 0) else ev.cents(price)
            event = Event(fan_id, message_id, item.get('createdAt'), 'out', text,
                          tuple(ev.numeric(m.get('id')) for m in media), price_cents).normalized()
            # Historical creator context does not trigger live work or takeover.
            # Use the existing immutable ingress and its duplicate/conflict fences.
            recorded = self.store._record(s, runtime, event, event.payload(), 'import', now)
            if recorded == 'backpressure':
                raise Refused('outgoing history requires intake capacity before completion')
            digest = ev.digest(item)
            price = item.get('price')
            if item.get('isOpened') is True and price not in (None, 0) and float(price) > 0:
                changed = item.get('changedAt') or item.get('createdAt')
                ev.add(s, account, dedupe=f'unlock:{message_id}', kind='unlock', source='history', event_key=None,
                       fan_id=fan_id, message_id=message_id, amount=ev.cents(price), occurred=ev.stamp(changed),
                       raw_sha256=digest, now=now)
                self._drive_after(account, fan_id, after)
        for action in after:
            action()
        return 'ok'
