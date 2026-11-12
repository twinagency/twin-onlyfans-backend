"""Read-only ownership derived from imported history: owned, unknown or clear_as_of; never "unowned".

A missing record is not proof that a fan does not own something. For each media of one fan this module returns:

  owned         proven: the fan opened a card whose previews are known (owned = media minus previews), or the engine's
                own positive sources say so (delivered purchase, accepted offer with a confirmed unlock).
  unknown       not provable either way: the card's previews are unknown or its facts disagree, an unlock names a message
                we never saw, a payment has no unlock to explain it, a purchase was reversed and no person resolved it, or
                the fan's coverage is not complete (see fans_api_adapter.history_import.fan_state).
  clear_as_of   only when the fan's coverage is complete and no evidence names the media; the time is when coverage
                completed. Later purchases arrive as webhooks and flip the state to owned.

The latest-1,000 vault window is a catalogue selection. It is never consulted here, so being in it (or rotating out
of it) proves nothing about ownership. Unknown blocks only a NEW PAID card of that product to that fan.
This module only reads. Whoever calls it decides what to do with the answer.
"""
from dataclasses import dataclass
import json

from sqlalchemy import or_, select

from db.models import Message
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_fans_intake_models import OfFansEvidence
from db.onlyfans_history_models import OfFansMessageFact
from onlyfans_runtime.store import Refused

OWNED, UNKNOWN, CLEAR = "owned", "unknown", "clear_as_of"
# A reversal is resolved when a person dismissed it or the canonical owner applied it.
_RESOLVED = ("applied", "dismissed")


@dataclass(frozen=True)
class MediaOwnership:
    media_id: int
    state: str
    as_of: str | None
    reasons: tuple
    evidence: tuple


def _ids(raw):
    try:
        value = json.loads(raw or "[]")
        return [int(v) for v in value] if type(value) is list else []
    except (TypeError, ValueError):
        return []


def derive(s, account, fan_id, media_ids):
    """{media_id: MediaOwnership} for one fan of one account. Account scoped by construction."""
    from fans_api_adapter import history_import
    if type(fan_id) is not int or type(media_ids) not in (list, tuple, set, frozenset) or len(media_ids) > 2000 \
            or any(type(m) is not int for m in media_ids):
        raise Refused("invalid ownership query")
    media = sorted(set(media_ids))
    coverage = history_import.fan_state(s, account, fan_id)
    owned, unknown, evidence, fan_level = {}, {}, {}, set()

    def note(target, key, reason=None, ref=None):
        bucket = target.setdefault(key, set())
        if reason:
            bucket.add(reason)
        if ref:
            evidence.setdefault(key, set()).add(ref)

    # 1. positive sources the engine already keeps (delivered purchases, confirmed unlocks of our own offers)
    from ownership import owned_media_in_session
    try:
        for m in owned_media_in_session(s, account, fan_id, limit=10000):
            note(owned, m, ref="engine")
    except ValueError:
        fan_level.add("ownership_evidence_unavailable")
    policy = s.get(OfDeliveryPolicy, account)
    if policy is not None and policy.transport_kind == "fansapi":
        from conversation_engine.commerce import provider_unlocked_media
        from db.onlyfans_models import OfRuntime
        runtime = s.get(OfRuntime, account)
        if runtime is not None:
            try:
                for m in provider_unlocked_media(s, runtime, fan_id):
                    note(owned, int(m), ref="engine")
            except Refused:
                fan_level.add("ownership_evidence_unavailable")
    # 2. imported history: unlock evidence and opened card facts, message by message
    unlocks = set(s.scalars(select(OfFansEvidence.message_id).where(OfFansEvidence.account_id == account,
        OfFansEvidence.fan_id == fan_id, OfFansEvidence.kind == "unlock", OfFansEvidence.status != "dismissed",
        OfFansEvidence.message_id.is_not(None))))
    facts = {f.message_id: f for f in s.scalars(select(OfFansMessageFact).where(OfFansMessageFact.account_id == account,
        OfFansMessageFact.fan_id == fan_id, or_(OfFansMessageFact.is_opened.is_(True), OfFansMessageFact.message_id.in_(unlocks),
            OfFansMessageFact.preview_ids_json.is_(None), OfFansMessageFact.conflicted.is_(True))))}
    opened = unlocks | {mid for mid, f in facts.items()
                        if f.is_opened is True or f.preview_ids_json is None or f.conflicted}
    ours = set(s.scalars(select(OfTextIntent.provider_message_id).where(OfTextIntent.account_id == account,
        OfTextIntent.provider_message_id.in_(opened)))) if opened else set()
    explained = set()
    for mid in sorted(opened):
        fact, ref = facts.get(mid), "message:%d" % mid
        if fact is not None and fact.is_tip is True:
            continue                               # a tip is not an unlock of media
        explained.add(mid)
        if fact is None:
            if mid in ours:
                continue                           # our own send: the positive sources above are authoritative
            seen = s.get(Message, (account, fan_id, mid))
            if seen is None:
                fan_level.add("unlock_message_unseen")
            for m in (_ids(seen.media_ids) if seen is not None else ()):
                note(unknown, m, "unlock_terms_unknown", ref)
            continue
        ids = _ids(fact.media_ids_json)
        if fact.conflicted:
            for m in ids:
                note(unknown, m, "fact_conflict", ref)
        elif fact.preview_ids_json is None:
            for m in ids:
                note(unknown, m, "previews_unknown", ref)
        else:
            previews = set(_ids(fact.preview_ids_json))
            for m in ids:
                if m not in previews:              # the whole card was bought; free previews were only shown
                    note(owned, m, ref=ref)
    # 3. a reversal nobody resolved: whatever it may have reversed is unknown, never silently still owned
    reversed_open = s.scalar(select(OfFansEvidence.evidence_id).where(OfFansEvidence.account_id == account,
        OfFansEvidence.fan_id == fan_id, OfFansEvidence.kind == "chargeback", OfFansEvidence.status.not_in(_RESOLVED)).limit(1)) is not None
    if reversed_open:
        for m in list(owned):
            note(unknown, m, "reversal_unresolved")
        owned = {}
    # 4. a message payment with no unlock to explain it: we cannot say which media it bought
    payments = len(set(s.scalars(select(OfFansEvidence.provider_transaction_id).where(OfFansEvidence.account_id == account,
        OfFansEvidence.fan_id == fan_id, OfFansEvidence.kind == "transaction", OfFansEvidence.status != "dismissed"))))
    if payments > len(explained):
        fan_level.add("payment_without_unlock")
    out = {}
    for m in media:
        refs = tuple(sorted(evidence.get(m, ())))
        if m in owned:
            out[m] = MediaOwnership(m, OWNED, None, (), refs)
        elif m in unknown or fan_level:
            out[m] = MediaOwnership(m, UNKNOWN, None, tuple(sorted(unknown.get(m, set()) | fan_level)), refs)
        elif coverage["state"] != "complete":
            out[m] = MediaOwnership(m, UNKNOWN, None, ("coverage_" + coverage["state"],), refs)
        else:
            out[m] = MediaOwnership(m, CLEAR, coverage["asOf"], (), ())
    return out


def evaluate_card(s, account, fan_id, terms):
    """Decision for one NEW PAID card: ok, owned (any paid asset is owned) or unknown (any paid asset is unknown).

    Paid assets are the card's media minus its free previews. A bundle is never split: one owned or unknown paid
    asset decides the whole card, and no per-asset price is ever derived from it.
    """
    previews = set(terms.get("preview_ids") or ())
    hero = [int(m) for m in terms["media_ids"] if m not in previews]
    states = derive(s, account, fan_id, hero)
    owned = sorted(m for m, v in states.items() if v.state == OWNED)
    unknown = sorted(m for m, v in states.items() if v.state == UNKNOWN)
    return {"decision": "owned" if owned else "unknown" if unknown else "ok", "owned": owned, "unknown": unknown, "states": states}


def assert_card_allowed(s, account, fan_id, terms):
    """Refuse a new paid card whose paid assets are owned or of unknown ownership. Free content is never refused here.

    Only accounts that configured a history import are governed; every other account keeps its existing behaviour.
    """
    from fans_api_adapter import history_import
    if not history_import.started(s, account) or int(terms.get("price_cents") or 0) <= 0:
        return
    verdict = evaluate_card(s, account, fan_id, terms)
    if verdict["decision"] == "owned":
        raise Refused("bundle overlaps owned media")
    if verdict["decision"] == "unknown":
        raise Refused("ownership coverage unknown")
