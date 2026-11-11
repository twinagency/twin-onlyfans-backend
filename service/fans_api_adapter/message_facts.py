"""First-seen facts of creator pay-per-view messages, kept beside (never inside) the canonical message.

A history row's previews, locked text and queue origin are captured before message normalisation discards them,
using the offer-terms reader's semantics: a missing or unsupported value is unknown (NULL), an empty list is a
fact. A priced message is a fact about an ASK, not proof of a sale: whether anyone owns the media is derived
elsewhere (conversation_engine/history_ownership.py). Facts are first-seen and frozen by the database except
for the one legitimate transition, a purchase observed later (is_opened False/unknown -> True).
"""
import json

from db.onlyfans_history_models import OfFansMessageFact
from onlyfans_runtime.offer_terms import from_history
from onlyfans_runtime.store import Refused
from . import payment_evidence as ev


def _flag(row, key):
    value = row.get(key)
    return value if type(value) is bool else None


def record(s, account, fan_id, message_id, row, event, *, via, now):
    """Insert or refresh the fact for one outgoing priced media message. Never raises for a row it cannot use."""
    if type(row) is not dict or event.direction != "out" or event.price_cents <= 0 or not event.media_ids:
        return None
    try:
        observation = from_history(row, event)
    except Refused:
        observation = None
    previews = None if observation is None or observation.preview_ids is None else list(observation.preview_ids)
    locked = None if observation is None else observation.locked_text
    opened = _flag(row, "isOpened")
    try:
        opened_at = ev.stamp(row.get("changedAt") or row.get("createdAt")) if opened else None
    except Refused:
        opened_at = None
    media = json.dumps(list(event.media_ids))
    existing = s.get(OfFansMessageFact, (account, fan_id, message_id), with_for_update=True)
    if existing is None:
        s.add(OfFansMessageFact(account_id=account, fan_id=fan_id, message_id=message_id, price_cents=event.price_cents,
            media_ids_json=media, preview_ids_json=None if previews is None else json.dumps(previews), locked_text=locked,
            from_queue=_flag(row, "isFromQueue"), is_opened=opened, opened_at=opened_at, is_tip=_flag(row, "isTip"),
            conflicted=False, via=via, raw_sha256=ev.digest(row), sent_at=ev.stamp(event.created_at), created_at=now))
        s.flush()
        return "new"
    if (existing.price_cents, existing.media_ids_json) != (event.price_cents, media):
        existing.conflicted = True      # the first-seen terms stay; the disagreement is what coverage must not trust
        return "conflict"
    if opened is True and existing.is_opened is not True:
        existing.is_opened, existing.opened_at = True, opened_at
    return "duplicate"


def mark_opened(s, account, fan_id, message_id, occurred):
    """A messages.ppv.unlocked event names the message: record the opening on its fact, if we hold one."""
    fact = s.get(OfFansMessageFact, (account, fan_id, message_id), with_for_update=True)
    if fact is not None and fact.is_opened is not True:
        fact.is_opened, fact.opened_at = True, occurred
