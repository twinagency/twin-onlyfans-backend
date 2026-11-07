"""Use the dashboard's shared fan-memory projection with binding fencing."""
from hashlib import sha256
import json
from sqlalchemy import select
from db.models import Message, OfCreatorBinding
from db.onlyfans_engine_models import OfEngineMemoryBinding, OfEngineRequest
from onlyfans_runtime.launch_history_memory.memory import snapshot as shared_memory_snapshot
from onlyfans_runtime.store import utcnow
from .history_scope import has_unscoped_messages


def projection(s, runtime, fan_id, source):
    prior_binding = s.scalar(select(OfCreatorBinding.id).where(
        OfCreatorBinding.account_id == runtime.account_id, OfCreatorBinding.id != runtime.binding_id).limit(1))
    if prior_binding is not None:
        result = {'state':'unavailable', 'reason':'prior_binding_requires_review'}
    elif has_unscoped_messages(s, runtime.account_id, fan_id, runtime.binding_id):
        result = {'state':'unavailable', 'reason':'unscoped_history_requires_review'}
    else:
        memory = shared_memory_snapshot(s, runtime.account_id, fan_id, query=source.body[:2000])
        result = {'state':'current', 'memory':memory}
    canonical = json.dumps({'binding_id':runtime.binding_id, 'projection':result},
        sort_keys=True, separators=(',', ':'), ensure_ascii=False, default=str)
    return result, sha256(canonical.encode('utf-8')).hexdigest()


def bind(s, runtime, decision, source, view, fingerprint, offer_fingerprint):
    s.add(OfEngineMemoryBinding(account_id=runtime.account_id, decision_id=decision.decision_id,
        fan_id=decision.fan_id, source_message_id=source.message_id, binding_id=runtime.binding_id,
        source_sha256=sha256(source.body.encode('utf-8')).hexdigest(), status=view['state'], fingerprint=fingerprint, offer_fingerprint=offer_fingerprint, created_at=utcnow()))


def current(s, runtime, decision):
    binding = s.get(OfEngineMemoryBinding, (runtime.account_id, decision.decision_id))
    request = s.get(OfEngineRequest, (runtime.account_id, decision.request_id))
    if (binding is None or request is None or binding.binding_id != runtime.binding_id
            or binding.fan_id != decision.fan_id or binding.source_message_id != request.source_message_id):
        return False
    source = s.get(Message, (runtime.account_id, decision.fan_id, request.source_message_id))
    if (source is None or source.is_unsent or source.direction != 'in'
            or sha256(source.body.encode('utf-8')).hexdigest() != binding.source_sha256):
        return False
    view, fingerprint = projection(s, runtime, decision.fan_id, source)
    from .offers import match as match_offers
    offer_view = match_offers(s, runtime, decision.fan_id, source.body)
    from .catalogue import search_context
    catalogue_view = search_context(s, runtime, decision.fan_id, source)
    offer_fingerprint = sha256(json.dumps({'offers':offer_view, 'catalogue':catalogue_view},
        sort_keys=True, separators=(',', ':'), ensure_ascii=False, default=str).encode()).hexdigest()
    return (binding.status == view['state'] and binding.fingerprint == fingerprint
            and binding.offer_fingerprint == offer_fingerprint)
