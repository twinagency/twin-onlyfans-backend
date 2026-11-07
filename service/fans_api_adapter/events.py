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

from sqlalchemy import select
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
                  'accounts.authentication_failed', 'accounts.disconnected'}
SUPPORTED = ACCOUNT_EVENTS | {'messages.received', 'messages.sent', 'messages.ppv.unlocked',
                              'transactions.new', 'chargebacks.new', 'subscriptions.new'}
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


class FansEventIntake:
    def __init__(self, store, *, human_grace_seconds=30, pair_window_seconds=1800, mismatch_settle_seconds=240):
        self.store, self.engine = store, store.engine
        self.grace = timedelta(seconds=human_grace_seconds)
        self.window = timedelta(seconds=pair_window_seconds)
        self.settle = timedelta(seconds=mismatch_settle_seconds)

    # ------------------------------------------------------------------ intake
    def accept(self, raw_body, signature, secret, event_key):
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
            connection = self._resolve(s, body, event, now)
            if connection is None:
                raise Refused('provider account is not bound')
            account, epoch = connection.account_id, connection.epoch
            existing = s.get(OfFansWebhook, (account, epoch, event_key))
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
        return {'outcome': outcome, 'account': account, 'epoch': epoch, 'projection': status}

    def _resolve(self, s, body, event, now):
        """Find the connection row for this delivery; a reconnect (new acct_) re-binds by creator id."""
        provider_id = body['account_id']
        connection = s.scalar(select(OfFansConnection).where(OfFansConnection.provider_account_id == provider_id)
                              .order_by(OfFansConnection.epoch.desc()).limit(1))
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
        if len(accounts) != 1:
            return None  # unknown or ambiguous creator: never invent a binding
        account = accounts.pop()
        latest = prior[0]
        policy = s.get(OfDeliveryPolicy, account)
        if policy is None or policy.transport_kind not in ALLOWED_TRANSPORTS:
            raise Refused('webhook intake is limited to the synthetic lab')
        binding = s.get(OfCreatorBinding, latest.binding_id)
        runtime = self.store._runtime(s, binding.tenant_id, account)
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
            row = s.get(OfFansProjection, (account, epoch, event_key), with_for_update=True)
            if row is None or row.status != 'pending':
                return row.status if row is not None else 'missing'
            raw = s.get(OfFansWebhook, (account, epoch, event_key))
            body = _parse(raw.raw_body)
            connection = s.get(OfFansConnection, (account, epoch))
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
        if event == 'subscriptions.new':
            return self._subscription(s, payload, connection)
        if event == 'messages.ppv.unlocked':
            return self._unlock(s, payload, raw, connection, now, after)
        if event == 'transactions.new':
            return self._transaction(s, payload, raw, connection, now, after, source='webhook')
        if event == 'chargebacks.new':
            return self._chargeback(s, payload, raw.event_key, ev.digest(json.loads(raw.raw_body)), connection, now, after, 'webhook')
        raise Refused('unsupported event')

    # ---------------------------------------------------------------- accounts
    def _account(self, s, event, payload, provider_id, current, connection, link, now, after):
        if provider_id != current and event != 'accounts.reconnected' and event != 'accounts.connected':
            return 'ignored', 'superseded_account'  # a late event about a replaced acct_ id
        creator = payload.get('onlyfans_id')
        if creator is not None and str(creator) != link.creator_id:
            raise Refused('account event names another creator')
        if event in ('accounts.connected', 'accounts.reconnected'):
            if provider_id == current:
                link.provider_account_id = provider_id
                if link.state != 'connected':
                    link.state = 'connected'
            return 'projected', 'connected'
        if event == 'accounts.session_expired':
            link.session_refreshes += 1  # the provider re-connects it itself: informational only
            return 'projected', 'session_refreshing'
        link.state = 'auth_failed' if event == 'accounts.authentication_failed' else 'disconnected'
        link.send_blocked, link.block_reason, link.updated_at = True, link.state, now
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
            # provider): surface it for a person instead of ignoring it.
            owner = s.get(OfEngineOwner, (connection.account_id, fan_id))
            if owner is not None and owner.mode == 'active':
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
    def drive(self, account, fan_id):
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
                ready = ev.linked_unlocks(s, account)
                charged = ev.pending_chargebacks(s, account)
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
            for row in rows:
                own = s.scalar(select(OfTextIntent.intent_id).where(OfTextIntent.account_id == account,
                    OfTextIntent.provider_message_id == row.message_id))
                if own is not None:
                    row.status, row.reason, row.intent_id, row.updated_at = 'dismissed', 'own_send', own, now
                    continue
                unresolved = s.scalar(select(OfTextIntent.intent_id).where(OfTextIntent.account_id == account,
                    OfTextIntent.fan_id == fan_id, OfTextIntent.status.in_(('dispatching', 'unknown'))).limit(1))
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
            binding = s.get(OfCreatorBinding, runtime.binding_id)
            raw = s.scalar(select(OfFansWebhook.raw_body).where(OfFansWebhook.account_id == account,
                OfFansWebhook.event_key == row.event_key)) if row.event_key else None
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
            revision = state.revision if state is not None else 0
            already = state is not None and (state.mode == 'human_controlled' or state.takeover_state != 'none')
        if already:
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
        """One history message: inbound text joins canonical ingress; a purchased PPV (isOpened with a price) is unlock evidence.
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
            connection = s.scalar(select(OfFansConnection).where(OfFansConnection.account_id == account,
                OfFansConnection.status == 'active'))
            if connection is None:
                raise Refused('no active provider connection')
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
