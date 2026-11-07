"""Persist authenticated synthetic Fans API intake before acknowledgement.

No HTTP listener, network client, real credentials or live connection is
provided. The adapter records webhook evidence and the canonical ingress in a
single transaction. Unknown/unsupported identities fail closed.
"""
from datetime import datetime
from hashlib import sha256
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_engine_models import OfFansConnection, OfFansWebhook, OfFansWebhookConflict
from onlyfans_runtime.store import Event, Refused, utcnow
from .webhooks import verify_signature


def _numeric_id(value):
    if type(value) is int:
        result = value
    elif type(value) is str and value.isascii() and value.isdecimal() and len(value) <= 19:
        result = int(value)
    else:
        raise Refused('provider identity is not a supported numeric id')
    if not 0 < result < 2**63:
        raise Refused('provider identity is outside the supported range')
    return result


def _unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError('duplicate JSON key')
        obj[key] = value
    return obj


def _message(body):
    if type(body) is not dict or body.get('event') != 'messages.received' or type(body.get('payload')) is not dict:
        raise Refused('unsupported webhook event')
    payload = body['payload']
    sender = payload.get('fromUser')
    fan = payload.get('fan')
    if type(sender) is not dict or 'id' not in sender:
        raise Refused('incoming sender is missing')
    fan_id = _numeric_id(sender['id'])
    if fan is not None and (type(fan) is not dict or _numeric_id(fan.get('id')) != fan_id):
        raise Refused('fan identity conflicts')
    if payload.get('isSentByMe') is True:
        raise Refused('incoming message direction conflicts')
    text = payload.get('text')
    if type(text) is not str:
        raise Refused('incoming message text is unavailable')
    media = payload.get('media', [])
    if type(media) is not list:
        raise Refused('incoming media is malformed')
    ids = []
    for item in media:
        if type(item) is not dict:
            raise Refused('incoming media identity is malformed')
        ids.append(_numeric_id(item.get('id')))
    if type(payload.get('price', 0)) is bool or payload.get('price', 0) not in (0, None):
        raise Refused('incoming price mapping requires provider qualification')
    return Event(fan_id, _numeric_id(payload.get('id')), payload.get('createdAt'), 'in', text, tuple(ids), 0).normalized()


class FansWebhookAdapter:
    def __init__(self, store):
        self.store = store
        self.engine = store.engine

    def bind_simulated(self, tenant, account, provider_account_id, provider_creator_id):
        if (type(provider_account_id) is not str or not provider_account_id.startswith('acct_')
                or len(provider_account_id) > 255 or type(provider_creator_id) is not str or not provider_creator_id.strip()
                or len(provider_creator_id) > 255):
            raise Refused('invalid provider connection identity')
        with Session(self.engine) as s, s.begin():
            runtime = self.store._runtime(s, tenant, account)
            policy = s.get(OfDeliveryPolicy, account)
            if policy is None or policy.transport_kind != 'synthetic':
                raise Refused('provider binding is limited to the synthetic lab')
            active = s.scalar(select(OfFansConnection).where(OfFansConnection.account_id == account,
                                                              OfFansConnection.status == 'active').with_for_update())
            if active and (active.provider_account_id, active.provider_creator_id, active.binding_id) == (
                    provider_account_id, provider_creator_id, runtime.binding_id):
                return active.epoch
            if active:
                unresolved = s.scalar(select(OfTextIntent.intent_id).where(OfTextIntent.account_id == account,
                    OfTextIntent.status.in_(('dispatching', 'unknown'))).limit(1))
                if unresolved:
                    raise Refused('reconcile unresolved sends before reconnecting')
                active.status = 'retired'
                s.flush()
            epochs = s.scalars(select(OfFansConnection.epoch).where(OfFansConnection.account_id == account)).all()
            epoch = max(epochs, default=0) + 1
            s.add(OfFansConnection(account_id=account, epoch=epoch, binding_id=runtime.binding_id,
                provider_account_id=provider_account_id, provider_creator_id=provider_creator_id,
                status='active', created_at=utcnow()))
            return epoch

    def accept(self, raw_body, signature, secret, event_key, *, expected_tenant=None, expected_account=None):
        if (expected_tenant is None) != (expected_account is None):
            raise Refused('complete expected scope required')
        if (type(raw_body) is not bytes or len(raw_body) > 1_000_000 or not verify_signature(raw_body, signature, secret)
                or type(event_key) is not str or not 1 <= len(event_key) <= 255 or event_key != event_key.strip()):
            raise Refused('webhook authentication or identity failed')
        try:
            body = json.loads(raw_body.decode('utf-8'), object_pairs_hook=_unique_object)
        except (UnicodeError, ValueError, TypeError) as exc:
            raise Refused('invalid webhook payload') from exc
        provider_id = body.get('account_id') if type(body) is dict else None
        if type(provider_id) is not str:
            raise Refused('provider account is missing')
        with Session(self.engine) as s, s.begin():
            connection = s.scalar(select(OfFansConnection).where(OfFansConnection.provider_account_id == provider_id,
                                                                  OfFansConnection.status == 'active'))
            if connection is None:
                raise Refused('provider account is not bound')
            # Locate the creator-scoped runtime before persisting anything.
            from db.onlyfans_models import OfRuntime
            from db.models import OfCreatorBinding
            runtime_row = s.get(OfRuntime, connection.account_id)
            binding = s.get(OfCreatorBinding, connection.binding_id)
            if runtime_row is None or binding is None:
                raise Refused('provider binding is unavailable')
            if expected_account is not None and (connection.account_id != expected_account or binding.tenant_id != expected_tenant):
                raise Refused('webhook account scope mismatch')
            policy = s.get(OfDeliveryPolicy, connection.account_id)
            if policy is None or policy.transport_kind != 'synthetic':
                raise Refused('webhook intake is limited to the synthetic lab')
            runtime = self.store._runtime(s, binding.tenant_id, connection.account_id)
            connection = s.get(OfFansConnection, (connection.account_id, connection.epoch), with_for_update=True)
            if connection.status != 'active' or connection.binding_id != runtime.binding_id:
                raise Refused('provider binding changed')
            digest = sha256(raw_body).hexdigest()
            existing = s.get(OfFansWebhook, (connection.account_id, connection.epoch, event_key))
            if existing is not None:
                if existing.payload_sha256 != digest:
                    key = (connection.account_id, connection.epoch, event_key, digest)
                    if s.get(OfFansWebhookConflict, key) is None:
                        s.add(OfFansWebhookConflict(account_id=connection.account_id, epoch=connection.epoch,
                            event_key=event_key, payload_sha256=digest, raw_body=raw_body, received_at=utcnow()))
                    return 'quarantined'
                return existing.status if existing.status == 'quarantined' else 'duplicate'
            try:
                message = _message(body)
            except (Refused, ValueError, TypeError):
                s.add(OfFansWebhook(account_id=connection.account_id, epoch=connection.epoch,
                    event_key=event_key, event_type=str(body.get('event') or 'unknown')[:80],
                    payload_sha256=digest, raw_body=raw_body, status='quarantined', received_at=utcnow()))
                return 'quarantined'
            result = self.store._record(s, runtime, message, message.payload(), 'ws', utcnow())
            if result == 'backpressure':
                raise Refused('inbox backpressure; retry later')
            s.add(OfFansWebhook(account_id=connection.account_id, epoch=connection.epoch,
                event_key=event_key, event_type='messages.received', payload_sha256=digest,
                raw_body=raw_body, status='accepted', received_at=utcnow()))
            return 'accepted' if result == 'accepted' else 'duplicate'
