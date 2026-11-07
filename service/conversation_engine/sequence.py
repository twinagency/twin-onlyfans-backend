"""Ordered reply parts share a request but use separate canonical intents."""
from sqlalchemy import select
from db.models import Message
from db.onlyfans_delivery_models import OfTextIntent
from db.onlyfans_engine_models import OfEngineDecision


def prior_state(s, runtime, decision, state):
    if state is None or state.revision != decision.control_revision:
        return 'conversation_changed'
    if decision.part_index == 0:
        return None if state.input_revision == decision.input_revision else 'conversation_changed'
    previous = list(s.scalars(select(OfEngineDecision).where(OfEngineDecision.account_id == runtime.account_id,
        OfEngineDecision.request_id == decision.request_id, OfEngineDecision.generation == decision.generation,
        OfEngineDecision.part_index < decision.part_index)
        .order_by(OfEngineDecision.part_index)))
    if [row.part_index for row in previous] != list(range(decision.part_index)) or any(row.status != 'completed' for row in previous):
        return 'predecessor_incomplete'
    if state.input_revision != decision.input_revision + decision.part_index:
        return 'conversation_changed'
    for part in previous:
        intent = s.get(OfTextIntent, (runtime.account_id, part.intent_id)) if part.intent_id else None
        if (intent is None or intent.status != 'accepted' or intent.projected_at is None
                or intent.binding_id != runtime.binding_id or intent.fan_id != decision.fan_id
                or intent.payload_hash != part.payload_hash or intent.body != part.body or not intent.provider_message_id):
            return 'predecessor_incomplete'
        message = s.get(Message, (runtime.account_id, decision.fan_id, intent.provider_message_id))
        if message is None or message.direction != 'out' or message.is_unsent or message.body != part.body:
            return 'predecessor_incomplete'
    return None
