"""Persist authenticated synthetic Fans API intake before acknowledgement.

The adapter records webhook evidence and canonical ingress in one transaction.
The separate local HTTP lab uses only synthetic credentials and loopback I/O. Unknown/unsupported identities fail closed.
"""
from datetime import datetime
from hashlib import sha256
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.onlyfans_delivery_models import ENGINE_TRANSPORT_KINDS, OfDeliveryPolicy, OfTextIntent
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


def _nonfinite(_):
    raise ValueError('nonfinite JSON value')


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
            if policy is None or policy.transport_kind not in ENGINE_TRANSPORT_KINDS:
                raise Refused('provider binding is limited to the synthetic lab')
            return self._bind_connection(s, runtime, provider_account_id, provider_creator_id)

    def _bind_connection(self, s, runtime, provider_account_id, provider_creator_id):
        """Shared connection owner; caller holds runtime and supplies verified scope."""
        account = runtime.account_id
        active = s.scalar(select(OfFansConnection).where(OfFansConnection.account_id == account,
                                                          OfFansConnection.status == 'active').with_for_update())
        if active and (active.provider_account_id, active.provider_creator_id, active.binding_id) == (
                provider_account_id, provider_creator_id, runtime.binding_id):
            return active.epoch
        if active:
            unresolved = s.scalar(select(OfTextIntent.intent_id).where(OfTextIntent.account_id == account,
                OfTextIntent.status.in_(('dispatching', 'unknown'))).limit(1))
            from db.onlyfans_engine_models import OfEngineIndependentAdmission
            independent=s.scalar(select(OfEngineIndependentAdmission.attempt_id).where(
                OfEngineIndependentAdmission.account_id==account,
                OfEngineIndependentAdmission.status.in_(("dispatching","unknown"))).limit(1))
            if unresolved is not None or independent is not None:
                raise Refused('reconcile unresolved sends before reconnecting')
            active.status = 'retired'
            s.flush()
        epochs = s.scalars(select(OfFansConnection.epoch).where(OfFansConnection.account_id == account)).all()
        epoch = max(epochs, default=0) + 1
        s.add(OfFansConnection(account_id=account, epoch=epoch, binding_id=runtime.binding_id,
            provider_account_id=provider_account_id, provider_creator_id=provider_creator_id,
            status='active', created_at=utcnow()))
        return epoch

    def bind_verified(self, tenant, account, client, *, guard):
        """Identity-read proof under disabled preparation, never a caller assertion."""
        from .protocol import FansApiClient
        from conversation_engine.preparation import PreparationRequestGuard,fingerprint,encoded
        from db.onlyfans_fans_intake_models import OfFansLink
        from onlyfans_runtime.launch_operations_models import OfLaunchCommand
        if (type(client) is not FansApiClient or not client._identity_verified
                or type(guard) is not PreparationRequestGuard or client._request_guard is not guard
                or guard.store is not self.store or (guard.tenant,guard.account)!=(tenant,account)):
            raise Refused("verified preparation client required")
        with Session(self.engine) as s,s.begin():
            runtime,scope,config=guard.ensure_current(s)
            if (client.account,client.creator_id)!=(config["provider_account"],config["creator_id"]):
                raise Refused("preparation identity mismatch")
            epoch=self._bind_connection(s,runtime,client.account,client.creator_id)
            if epoch!=client.epoch:raise Refused("preparation connection changed during proof")
            link=s.get(OfFansLink,(account,client.creator_id))
            if link is None:
                link=OfFansLink(account_id=account,creator_id=client.creator_id,provider_account_id=client.account,
                    state="connected",send_blocked=True,block_reason="preparation_only",
                    session_refreshes=0,reconnect_count=0,updated_at=utcnow())
                s.add(link)
            else:
                if link.provider_account_id!=client.account:link.reconnect_count+=1
                link.provider_account_id=client.account;link.state="connected";link.send_blocked=True
                if link.block_reason is None:link.block_reason="preparation_only"
                link.updated_at=utcnow()
            key=fingerprint(["preparation-connect",runtime.binding_id,scope.revision,epoch,client.account,client.creator_id])
            if s.get(OfLaunchCommand,(account,key)) is None:
                result={"epoch":epoch,"provider_account":client.account,"creator_id":client.creator_id,"identityVerified":True}
                s.add(OfLaunchCommand(account_id=account,command_key=key,binding_id=runtime.binding_id,
                    kind="preparation_connect",request_hash=key,actor="worker-import:"+fingerprint([tenant,account]),
                    reason="Budgeted provider identity read verified preparation connection",
                    before_json="{}",result_json=encoded(result),created_at=utcnow()))
            return epoch

    def accept(self, raw_body, signature, secret, event_key, *, expected_tenant=None, expected_account=None, expected_epoch=None, expected_creator_id=None):
        if ((expected_tenant is None) != (expected_account is None)
                or (expected_epoch is None) != (expected_creator_id is None)
                or (expected_epoch is not None and (expected_account is None or type(expected_epoch) is not int or expected_epoch < 1
                    or type(expected_creator_id) is not str or not expected_creator_id))):
            raise Refused('complete expected scope required')
        if (type(raw_body) is not bytes or len(raw_body) > 1_000_000 or not verify_signature(raw_body, signature, secret)
                or type(event_key) is not str or not 1 <= len(event_key) <= 255 or event_key != event_key.strip()):
            raise Refused('webhook authentication or identity failed')
        try:
            body = json.loads(raw_body.decode('utf-8'), object_pairs_hook=_unique_object, parse_constant=_nonfinite)
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
            if policy is None or policy.transport_kind not in ENGINE_TRANSPORT_KINDS:
                raise Refused('webhook intake is limited to the synthetic lab')
            runtime = self.store._runtime(s, binding.tenant_id, connection.account_id)
            connection = s.get(OfFansConnection, (connection.account_id, connection.epoch), with_for_update=True)
            if (connection.status != 'active' or connection.binding_id != runtime.binding_id
                    or expected_epoch is not None and (connection.epoch != expected_epoch or connection.provider_creator_id != expected_creator_id)):
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


    def accept_protocol_observation(self, client, raw_body, signature, secret, event_key, *, tenant, account):
        """Retain authenticated non-message observations, without inventing financial links.

        This isolated seam does not interpret an unlock as a purchase or update
        a canonical ledger. Authoritative mapping and coverage remain explicit.
        """
        from .protocol import FansApiClient
        if type(client) is not FansApiClient or not client._identity_verified:
            raise Refused('verified provider protocol required')
        observation = client.webhook(raw_body, signature=signature, secret=secret, event_key=event_key)
        if observation.state != 'observation':
            raise Refused('invalid protocol observation')
        if observation.data['event'] == 'messages.received':
            return self.accept(raw_body, signature, secret, event_key, expected_tenant=tenant, expected_account=account,
                               expected_epoch=client.epoch, expected_creator_id=client.creator_id)
        with Session(self.engine) as s, s.begin():
            runtime = self.store._runtime(s, tenant, account)
            policy = s.get(OfDeliveryPolicy, account)
            connection = s.get(OfFansConnection, (account, client.epoch), with_for_update=True)
            if (policy is None or policy.transport_kind not in ENGINE_TRANSPORT_KINDS or connection is None
                    or connection.status != 'active' or connection.binding_id != runtime.binding_id
                    or connection.provider_account_id != client.account or connection.provider_creator_id != client.creator_id):
                raise Refused('protocol observation scope mismatch')
            digest = sha256(raw_body).hexdigest()
            prior = s.get(OfFansWebhook, (account, client.epoch, event_key))
            if prior is not None:
                if prior.payload_sha256 == digest:
                    return 'duplicate' if prior.status == 'accepted' else 'quarantined'
                key = (account, client.epoch, event_key, digest)
                if s.get(OfFansWebhookConflict, key) is None:
                    s.add(OfFansWebhookConflict(account_id=account, epoch=client.epoch,
                        event_key=event_key, payload_sha256=digest, raw_body=raw_body, received_at=utcnow()))
                return 'quarantined'
            s.add(OfFansWebhook(account_id=account, epoch=client.epoch, event_key=event_key,
                event_type=observation.data['event'], payload_sha256=digest, raw_body=raw_body,
                status='accepted', received_at=utcnow()))
            return 'accepted'
