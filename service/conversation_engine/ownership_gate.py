"""One ownership decision for selection, preparation and the final check (Step 3).

Known ownership excludes a whole product: a bundle is excluded when ANY of its media (paid or preview, the engine's stricter rule)
is owned. Unknown ownership is never read as "unowned": it blocks every candidate for that fan until evidence is repaired.
"""
import ownership as _ownership
from sqlalchemy import select

from db.models import ContentOffer, Message, VaultSend
from db.onlyfans_delivery_models import OfDeliveryPolicy
from onlyfans_runtime.store import Refused

OWNED = 'bundle overlaps owned media'
UNKNOWN = 'ownership evidence unavailable'


def unmapped_paid_messages(s, account, fan_id, *, limit=50):
    """Paid (unlocked) messages whose media cannot be mapped to vault ids: the fan owns something we cannot name."""
    rows = s.execute(select(Message.message_id, Message.media_ids, Message.media_count, Message.price_cents).where(
        Message.account_id == account, Message.fan_id == fan_id, Message.direction == 'out', Message.is_paid.is_(True),
        Message.is_tip.is_(False), Message.is_unsent.is_(False)).limit(limit + 1)).all()
    if len(rows) > limit:
        return {None}
    unmapped = {mid for mid, media, count, price in rows if (media or '[]').strip() in ('', '[]', 'null')}
    if not unmapped:
        return set()
    # another recorded mapping (a purchased send or a delivered offer with catalogue fallback) names the media
    mapped = set(s.scalars(select(VaultSend.message_id).where(VaultSend.account_id == account, VaultSend.fan_id == fan_id,
        VaultSend.message_id.in_(unmapped), VaultSend.was_purchased.is_(True))))
    mapped |= {m for pair in s.execute(select(ContentOffer.offer_message_id, ContentOffer.delivery_message_id).where(
        ContentOffer.account_id == account, ContentOffer.fan_id == fan_id, ContentOffer.status == 'delivered')).all() for m in pair}
    return unmapped - mapped


def owned_state(s, account, fan_id):
    """(owned media ids, unknown reason or None). `unknown` set means no candidate may be offered to this fan."""
    from onlyfans_runtime import commerce as of_commerce
    if of_commerce.held(s, account, fan_id):
        return set(), 'commerce_hold'
    try:
        owned = set(_ownership.owned_media_in_session(s, account, fan_id, limit=10000))
    except ValueError:
        return set(), 'read_overflow'
    policy = s.get(OfDeliveryPolicy, account)
    if policy is not None and policy.transport_kind == 'fansapi':
        from db.onlyfans_fans_intake_models import OfFansEvidence
        from db.onlyfans_models import OfRuntime
        if s.scalar(select(OfFansEvidence.evidence_id).where(OfFansEvidence.account_id == account,
                OfFansEvidence.fan_id == fan_id, OfFansEvidence.kind == 'chargeback',
                OfFansEvidence.status.in_(('pending', 'held'))).limit(1)) is not None:
            return set(), 'unresolved_reversal'
        runtime = s.get(OfRuntime, account)
        if runtime is None:
            return set(), 'access_scope_unavailable'
        from .commerce import provider_unlocked_media
        try:
            owned |= provider_unlocked_media(s, runtime, fan_id)
        except Refused:
            return set(), 'access_evidence_unavailable'
    if unmapped_paid_messages(s, account, fan_id):
        return owned, 'unmapped_paid_evidence'
    return owned, None


def ownership_gate(s, account, fan_id, terms):
    owned, unknown = owned_state(s, account, fan_id)
    if unknown:
        raise Refused(f'{UNKNOWN}: {unknown}')
    if set(terms['media_ids']) & owned:
        raise Refused(OWNED)
