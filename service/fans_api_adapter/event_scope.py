"""One classification contract for authenticated webhook envelopes, plus bounded SQL recovery helpers.

Intake, interval recovery (ops/plan-c-supervision/validate_interval.py) and operator
reconciliation all call ``classify_raw``/``classify_body``. The event NAME is never enough: the
payload must have the documented shape, otherwise the envelope is ``UNKNOWN`` and stays held.

Classes
  SUPPORTED   the engine projects it (messages, payments, subscriptions, account lifecycle)
  IRRELEVANT  documented semantics prove it cannot change conversation, money, access or
              connection state for this scope (account counters, presence, likes)
  MATERIAL    documented to affect one identified fan (tips.received); holds that fan only
  UNKNOWN     malformed, undocumented, or documented-but-unverified (chat_queue.*): holds the account

Sources (https://docs.onlyfansapi.com/webhooks/available-events, read 2026-11-10):
  counters  - "{count: non-negative integer}" account counters; no chat, action or purchase identity.
  users.typing - payload {id}: a fan is typing. Presence only.
  users.online / users.offline - payload {fan, observed_at, status_changed_at, last_seen_online_at};
              detected by polling. Presence only.
  messages.liked / messages.unliked - {id, chat_id, fan_id, fan}: a like flag on a stored message.
              No text, media, price or recipient change.
  posts.liked - {id, type, text, user_id, post_id, fan, user}: a fan liked a creator post.
  tips.received - {id, amountGross, amountNet, ..., user_id(creator), fan{id}}: money from one fan.
  chat_queue.updated / finished - queue progress {id, total, pending, isDone, ...}. The documented
              fields do not say which fans received what, so impact is UNKNOWN and stays held.
  posts.new, fan_summary.*, media_uploads.*, data_exports.* - posts.new is not documented at all;
              the others were not verified as harmless to this scope and stay UNKNOWN.
"""
from typing import NamedTuple

from sqlalchemy import func

from onlyfans_runtime.store import Refused

ACCOUNT_EVENTS = ('accounts.connected','accounts.reconnected','accounts.disconnected',
                  'accounts.session_expired','accounts.authentication_failed',
                  'accounts.otp_code_required','accounts.face_otp_required')
SUBSCRIPTION_EVENTS = ('subscriptions.new', 'subscriptions.renewed', 'subscriptions.expired')

SUPPORTED, IRRELEVANT, MATERIAL, UNKNOWN = 'supported', 'irrelevant', 'material', 'unknown'


class Verdict(NamedTuple):
    cls: str
    event: object
    fan: object   # int for fan-identified events, else None
    reason: str


def event_fan(event_type, raw_body):
    # The schema-owned function catches malformed JSON and applies the same int64 bounds as intake.
    return func.of_engine_pilot_event_fan(raw_body, event_type)


# Documented account counters do not identify a chat, action, delivery or purchase.
# https://docs.onlyfansapi.com/webhooks/available-events#counter-updates
COUNTER_EVENTS = frozenset({'messages.unread_count_updated', 'messages.priority_count_updated',
    'messages.unread_tips_count_updated', 'calls.count_request_updated', 'notifications.count_updated'})


def _fan_id(value):
    if type(value) not in (int, str) or not str(value).isascii() or not str(value).isdecimal():
        raise ValueError('invalid fan')
    fan = int(value)
    if not 0 < fan < 2**63 or str(fan) != str(value):
        raise ValueError('invalid fan')
    return fan


def _optional_id(value):
    return None if value is None else _fan_id(value)


def _fan_object(value):
    if type(value) is not dict:
        raise ValueError('invalid fan object')
    return _fan_id(value.get('id'))


def _stamp_or_none(value):
    if value is not None and type(value) is not str:
        raise ValueError('invalid timestamp')


def _supported_fan(event, payload):
    """Fan (or None) for the engine-projected types; raises when the shape is not usable."""
    if event == 'messages.received':
        return _fan_id(payload['fromUser']['id'])
    if event == 'messages.sent':
        return _fan_id(payload['toUser']['id'])
    if event == 'messages.deleted':
        from fans_api_adapter.payment_evidence import numeric
        numeric(payload['id'])
        return None
    if event == 'messages.ppv.unlocked':
        return _fan_id(payload['chat_id'])
    if event in SUBSCRIPTION_EVENTS:
        return _fan_id(payload['fan']['id'])
    if event == 'transactions.new':
        return _fan_id((payload.get('fan') or payload['user'])['id'])
    if event == 'chargebacks.new':
        return _fan_id(payload['payment']['user']['id'])
    if event in ACCOUNT_EVENTS:
        return None
    raise ValueError('not a supported event')


def _irrelevant_fan(event, payload):
    """Validated-irrelevant shapes. Raises ValueError when the payload is not the documented shape."""
    if event in COUNTER_EVENTS:
        if set(payload) != {'count'} or type(payload['count']) is not int or payload['count'] < 0:
            raise ValueError('invalid counter')
        return None
    if event == 'users.typing':
        if set(payload) != {'id'}:
            raise ValueError('invalid typing')
        return _fan_id(payload['id'])
    if event in ('users.online', 'users.offline'):
        if 'fan' not in payload or not set(payload) <= {'fan', 'observed_at', 'status_changed_at', 'last_seen_online_at'}:
            raise ValueError('invalid presence')
        for key in ('observed_at', 'status_changed_at', 'last_seen_online_at'):
            _stamp_or_none(payload.get(key))
        return _fan_object(payload['fan'])
    if event in ('messages.liked', 'messages.unliked'):
        if 'id' not in payload or not set(payload) <= {'id', 'chat_id', 'fan_id', 'fan'}:
            raise ValueError('invalid like')
        _fan_id(payload['id'])
        _optional_id(payload.get('chat_id'))
        fan = _optional_id(payload.get('fan_id'))
        if payload.get('fan') is not None:
            fan = _fan_object(payload['fan'])
        return fan
    if event == 'posts.liked':
        if 'post_id' not in payload or not set(payload) <= {'id', 'type', 'text', 'user_id', 'post_id', 'fan', 'user'}:
            raise ValueError('invalid post like')
        _fan_id(payload['post_id'])
        return _fan_object(payload['fan']) if payload.get('fan') is not None else None
    raise ValueError('not a verified irrelevant event')


_IRRELEVANT_EVENTS = COUNTER_EVENTS | {'users.typing', 'users.online', 'users.offline',
                                       'messages.liked', 'messages.unliked', 'posts.liked'}
_SUPPORTED_EVENTS = frozenset(ACCOUNT_EVENTS + SUBSCRIPTION_EVENTS + ('messages.received', 'messages.sent',
    'messages.deleted', 'messages.ppv.unlocked', 'transactions.new', 'chargebacks.new'))


def classify_body(body):
    """Classify a parsed envelope ({event, account_id, payload}); never raises."""
    try:
        event, payload = body['event'], body['payload']
        if type(event) is not str or type(payload) is not dict:
            return Verdict(UNKNOWN, None, None, 'malformed_envelope')
    except (KeyError, TypeError):
        return Verdict(UNKNOWN, None, None, 'malformed_envelope')
    try:
        if event in _SUPPORTED_EVENTS:
            return Verdict(SUPPORTED, event, _supported_fan(event, payload), 'supported')
        if event in _IRRELEVANT_EVENTS:
            return Verdict(IRRELEVANT, event, _irrelevant_fan(event, payload), 'validated_irrelevant')
        if event == 'tips.received':
            return Verdict(MATERIAL, event, _fan_object(payload['fan']), 'tip_from_identified_fan')
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError, Refused):
        return Verdict(UNKNOWN, event, None, 'malformed_payload')
    return Verdict(UNKNOWN, event, None, 'unverified_event_type')


def classify_raw(raw):
    """Classify exact authenticated bytes; malformed JSON/envelope is UNKNOWN."""
    from fans_api_adapter.events import _parse
    try:
        body = _parse(raw)
    except Refused:
        return Verdict(UNKNOWN, None, None, 'malformed_envelope')
    return classify_body(body)


def known_irrelevant_event(body):
    return classify_body(body).cls == IRRELEVANT
