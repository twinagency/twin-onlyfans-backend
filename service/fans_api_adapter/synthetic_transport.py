"""In-process Fans API transport fixture wired to the single TextAuthority.

This class cannot make HTTP requests and is not a production adapter. It models
only the documented text-send outcome contract for deterministic lab tests.
"""
from datetime import datetime, timezone
from copy import deepcopy
import json
from decimal import Decimal
from sqlalchemy import select
from sqlalchemy.orm import Session

from conversation_engine.recovery import Outcome
from db.onlyfans_engine_models import OfEngineDecision, OfFansConnection
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from onlyfans_runtime.store import Refused
from onlyfans_runtime.text_delivery import Accepted, Dispatch, NotAccepted, TextAuthority
from .intake import _numeric_id
from .send_outcome import classify_send_error


class SyntheticFansProvider:
    def __init__(self, outcomes=('accepted',), *, message_id=9001):
        self.outcomes = list(outcomes)
        if type(message_id) is tuple:
            if not message_id or any(type(value) is not int or value <= 0 for value in message_id):
                raise ValueError('invalid synthetic message ids')
            self.message_ids, self.message_id = list(message_id), message_id[-1]
        else:
            self.message_ids, self.message_id = [], message_id
        self.calls = []
        self.accepted = {}

    def send(self, request):
        self.calls.append(request)
        if not self.outcomes:
            raise RuntimeError('synthetic provider outcome exhausted')
        outcome = self.outcomes.pop(0)
        if outcome == 'not_sent':
            return 503, {'error': 'SERVICE_UNAVAILABLE', 'request_outcome': 'not_sent'}
        if outcome == 'unknown':
            return 503, {'error': 'SERVICE_UNAVAILABLE', 'request_outcome': 'unknown'}
        if outcome == 'idempotency_unavailable':
            return 503, {'error': 'IDEMPOTENCY_UNAVAILABLE'}
        if outcome == 'mismatch':
            return 422, {'error': 'IDEMPOTENCY_KEY_MISMATCH'}
        if outcome not in {'accepted', 'accepted_lost'}:
            raise RuntimeError('invalid synthetic outcome')
        saved = self.accepted.get(request['idempotency_key'])
        if saved is None:
            message_id = self.message_ids.pop(0) if self.message_ids else self.message_id
            saved = {'data': {'id': message_id},
                '_fixture_scope': {'account_id': request['account_id'], 'chat_id': request['chat_id']},
                '_fixture_request': deepcopy(request)}
            self.accepted[request['idempotency_key']] = saved
        if outcome == 'accepted_lost':
            raise TimeoutError('synthetic accepted response lost')
        return 200, saved


class FansApiSyntheticTransport:
    def __init__(self, store, tenant_id, account_id, provider):
        if type(provider) is not SyntheticFansProvider:
            raise Refused('only the in-process provider fixture is available')
        self.store, self.tenant_id, self.account_id, self.provider = store, tenant_id, account_id, provider

    def send(self, dispatch, *, authority, tenant_id):
        if (type(self.provider) is not SyntheticFansProvider or type(authority) is not TextAuthority
                or type(dispatch) is not Dispatch or tenant_id != self.tenant_id):
            raise Refused('invalid synthetic transport context')
        with Session(self.store.engine) as s, s.begin():
            runtime = self.store._runtime(s, tenant_id, self.account_id)
            decision = s.scalar(select(OfEngineDecision).where(OfEngineDecision.account_id == self.account_id,
                                                                 OfEngineDecision.intent_id == dispatch.intent_id))
            connection = s.get(OfFansConnection, (self.account_id, decision.connection_epoch)) if decision and decision.connection_epoch is not None else None
            if (dispatch.account_id != self.account_id or decision is None or connection is None
                    or connection.status != 'active' or connection.binding_id != runtime.binding_id):
                raise Refused('synthetic provider connection or payload is unavailable')
            provider_account = connection.provider_account_id
        grant = authority.consume_dispatch(tenant_id, dispatch)
        if grant is None:
            raise Refused('dispatch is no longer eligible')
        payload = {'text': grant.body}
        if grant.media_ids:
            payload['mediaFiles'] = [str(value) for value in grant.media_ids]
            payload['previews'] = [str(value) for value in grant.preview_ids]
        if grant.price_cents:
            payload['price'] = Decimal(grant.price_cents) / Decimal(100)
        request = {'account_id': provider_account, 'chat_id': str(grant.fan_id),
                   'idempotency_key': grant.intent_id, 'body': payload}
        status, body = self._send_provider(request)
        proof = dict(account_id=grant.account_id, fan_id=grant.fan_id, intent_id=grant.intent_id,
            attempt_id=grant.attempt_id, payload_hash=grant.payload_hash,
            occurred_at=datetime.now(timezone.utc).isoformat(), evidence_ref='synthetic-fans:' + grant.attempt_id)
        if status == 200 and type(body) is dict and type(body.get('data')) is dict:
            scope = body.get('_fixture_scope')
            if scope != {'account_id': provider_account, 'chat_id': str(grant.fan_id)}:
                return None
            try:
                message_id = _numeric_id(body['data'].get('id'))
            except Refused:
                return None
            return Accepted(**proof, message_id=message_id)
        if classify_send_error(status, body) is Outcome.NOT_SENT:
            return NotAccepted(**proof)
        return None  # unknown, mismatch and conflict stay unresolved


    def _send_provider(self, request):
        if type(self.provider) is not SyntheticFansProvider:
            raise Refused('only the in-process provider fixture is available')
        return self.provider.send(request)

    def reconcile_accepted(self, intent_id):
        """Recover a saved synthetic acceptance without making another send.

        This fixture lookup is not a claim that Fans API offers such a lookup.
        Absence or conflicting identity is never treated as proof of failure.
        """
        if type(self.provider) is not SyntheticFansProvider:
            raise Refused('only the in-process provider fixture is available')
        authority = TextAuthority(self.store)
        with Session(self.store.engine) as s:
            runtime = self.store._runtime(s, self.tenant_id, self.account_id)
            policy = s.get(OfDeliveryPolicy, self.account_id)
            row = s.get(OfTextIntent, (self.account_id, intent_id))
            decision = s.scalar(select(OfEngineDecision).where(OfEngineDecision.account_id == self.account_id,
                                                                 OfEngineDecision.intent_id == intent_id))
            connection = s.get(OfFansConnection, (self.account_id, decision.connection_epoch)) if decision and decision.connection_epoch is not None else None
            if (policy is None or policy.transport_kind != 'synthetic' or row is None or decision is None
                    or connection is None or connection.binding_id != runtime.binding_id
                    or row.binding_id != runtime.binding_id or decision.payload_hash != row.payload_hash
                    or row.payload_hash != authority._intent_hash(row)):
                raise Refused('synthetic reconciliation scope is unavailable')
            if row.status == 'accepted':
                already_accepted = True
            elif row.status == 'unknown' and row.wire_started_at is not None and row.dispatch_token and row.attempt_id:
                already_accepted = False
                saved = self.provider.accepted.get(intent_id)
                body = {'text': row.body}
                media, previews = json.loads(row.media_json), json.loads(row.preview_json)
                if media:
                    body['mediaFiles'] = [str(value) for value in media]
                    body['previews'] = [str(value) for value in previews]
                if row.price_cents:
                    body['price'] = Decimal(row.price_cents) / Decimal(100)
                expected = {'account_id': connection.provider_account_id, 'chat_id': str(row.fan_id),
                            'idempotency_key': row.intent_id, 'body': body}
                if (type(saved) is not dict or saved.get('_fixture_request') != expected
                        or saved.get('_fixture_scope') != {'account_id': connection.provider_account_id, 'chat_id': str(row.fan_id)}
                        or type(saved.get('data')) is not dict):
                    return 'unknown'
                try:
                    message_id = _numeric_id(saved['data'].get('id'))
                except Refused:
                    return 'unknown'
                token = row.dispatch_token
                proof = Accepted(account_id=row.account_id, fan_id=row.fan_id, intent_id=row.intent_id,
                    attempt_id=row.attempt_id, payload_hash=row.payload_hash, message_id=message_id,
                    occurred_at=datetime.now(timezone.utc).isoformat(), evidence_ref='synthetic-reconcile:' + row.attempt_id)
            else:
                return row.status
        if not already_accepted:
            result = authority.record_accepted(self.tenant_id, self.account_id, intent_id, token, proof)
            if result != 'accepted':
                return result
        authority.materialize(self.tenant_id, self.account_id, intent_id)
        return 'accepted'
