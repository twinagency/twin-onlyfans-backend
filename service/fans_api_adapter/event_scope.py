"""Safe SQL expression for bounded recovery of authenticated webhook envelopes."""
from sqlalchemy import func

ACCOUNT_EVENTS = ('accounts.connected','accounts.reconnected','accounts.disconnected',
                  'accounts.session_expired','accounts.authentication_failed')


def event_fan(event_type, raw_body):
    # The schema-owned function catches malformed JSON and applies the same int64 bounds as intake.
    return func.of_engine_pilot_event_fan(raw_body, event_type)


# Documented account counters do not identify a chat, action, delivery or purchase.
# https://docs.onlyfansapi.com/webhooks/available-events#counter-updates
COUNTER_EVENTS = frozenset({'messages.unread_count_updated', 'messages.priority_count_updated',
    'messages.unread_tips_count_updated', 'calls.count_request_updated', 'notifications.count_updated'})


def known_irrelevant_event(body):
    if type(body) is not dict or type(body.get('event')) is not str or body['event'] not in COUNTER_EVENTS:
        return False
    payload = body.get('payload')
    return (type(payload) is dict and set(payload) == {'count'}
        and type(payload['count']) is int and payload['count'] >= 0)
