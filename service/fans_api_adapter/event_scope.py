"""SQL expressions for bounded recovery of already authenticated webhook envelopes."""
from sqlalchemy import case, cast, func
from sqlalchemy.dialects.postgresql import JSONB

ACCOUNT_EVENTS = ('accounts.connected','accounts.reconnected','accounts.disconnected',
                  'accounts.session_expired','accounts.authentication_failed')


def event_fan(event_type, raw_body):
    payload = cast(func.convert_from(raw_body, 'UTF8'), JSONB)['payload']
    return case(
        (event_type == 'messages.received', payload['fromUser']['id'].astext),
        (event_type == 'messages.sent', payload['toUser']['id'].astext),
        (event_type == 'messages.ppv.unlocked', payload['chat_id'].astext),
        (event_type == 'subscriptions.new', payload['fan']['id'].astext),
        (event_type == 'transactions.new', func.coalesce(payload['fan']['id'].astext, payload['user']['id'].astext)),
        (event_type == 'chargebacks.new', payload['payment']['user']['id'].astext),
        else_=None)
