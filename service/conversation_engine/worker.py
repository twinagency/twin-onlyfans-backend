"""A bounded offline worker connecting the existing inbox and authority.

Only the in-process model and provider fixtures are accepted. Replayed events
re-use their durable request, decision and intent; they never mint a new send.
"""
import json

from fans_api_adapter.intake import FansWebhookAdapter, _message
from fans_api_adapter.synthetic_transport import FansApiSyntheticTransport
from onlyfans_runtime.store import Refused
from onlyfans_runtime.text_delivery import TextAuthority
from .engine import EngineService


class SyntheticConversationWorker:
    def __init__(self, engine, adapter, transport):
        if (type(engine) is not EngineService or type(adapter) is not FansWebhookAdapter
                or type(transport) is not FansApiSyntheticTransport
                or engine.store is not adapter.store or engine.store is not transport.store):
            raise Refused('invalid synthetic worker dependencies')
        self.engine, self.adapter, self.transport = engine, adapter, transport

    def handle(self, tenant, account, *, raw_body, signature, secret, event_key):
        if (type(self.engine) is not EngineService or type(self.adapter) is not FansWebhookAdapter
                or type(self.transport) is not FansApiSyntheticTransport
                or self.engine.store is not self.adapter.store or self.engine.store is not self.transport.store
                or tenant != self.transport.tenant_id or account != self.transport.account_id):
            raise Refused('synthetic worker scope mismatch')
        intake = self.adapter.accept(raw_body, signature, secret, event_key,
                                     expected_tenant=tenant, expected_account=account)
        if intake == 'quarantined':
            return {'intake': intake, 'outcome': 'quarantined'}
        # Authentication and durable intake already succeeded; the decoded
        # message identity is used only to locate the canonical inbox row.
        message = _message(json.loads(raw_body.decode('utf-8')))
        self.engine.store.drain(tenant, account)
        decision = self.engine.plan(tenant, account, message.fan_id, message.message_id)
        intent = self.engine.queue(tenant, account, decision)
        outcome = TextAuthority(self.engine.store).dispatch(tenant, account, intent, self.transport)
        return {'intake': intake, 'decisionId': decision, 'intentId': intent, 'outcome': outcome}
