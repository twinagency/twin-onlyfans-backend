"""Reference outstanding offers without treating a reference as payment."""
import json
from sqlalchemy import and_, select
from db.models import ContentOffer, Message
from db.onlyfans_delivery_models import OfTextIntent
from db.onlyfans_engine_models import OfEngineAccessEvidence, OfEngineDecision, OfEngineOfferGrant
from .catalogue import aliases_for, normalize

_GENERIC = frozenset({'yes', 'yes please', 'send it', 'that one', 'i want it', 'buy it', 'ill take it'})


def match(s, runtime, fan_id, text, *, include_cards=False, exclude_intent_id=None):
    reference = _match(s, runtime, fan_id, text)
    if not include_cards:
        return reference
    facts = _cards(s, runtime, fan_id, exclude_intent_id=exclude_intent_id)
    if reference['state'] == 'incomplete':
        facts['cardsCoverage'] = 'incomplete'
    return {**reference, **facts}


def _cards(s, runtime, fan_id, *, exclude_intent_id=None):
    """Bounded scoped facts, independent of whether the current words name a card.

    Frozen terms and accepted receipt projection prove a sent card. Unknown or
    dispatching actions remain explicitly uncertain; access is separate from
    financial settlement. These facts grant no new offer or reference match.
    """
    from .commerce import _hash
    from onlyfans_runtime.commerce import held
    from db.onlyfans_delivery_models import OfDeliveryPolicy
    query = select(OfEngineDecision, OfTextIntent).join(OfTextIntent,
        and_(OfTextIntent.account_id == OfEngineDecision.account_id,
             OfTextIntent.intent_id == OfEngineDecision.intent_id)).where(
        OfEngineDecision.account_id == runtime.account_id, OfEngineDecision.fan_id == fan_id,
        OfEngineDecision.binding_id == runtime.binding_id, OfEngineDecision.action == 'offer',
        OfTextIntent.fan_id == fan_id, OfTextIntent.binding_id == runtime.binding_id,
        OfTextIntent.status.in_(('accepted', 'unknown', 'dispatching')))
    if exclude_intent_id is not None:
        query = query.where(OfTextIntent.intent_id != exclude_intent_id)
    rows = s.execute(query.order_by(OfTextIntent.created_at.desc(), OfTextIntent.intent_id).limit(11)).all()
    cards, complete = [], len(rows) <= 10
    policy = s.get(OfDeliveryPolicy, runtime.account_id)
    prefix = {'fansapi': 'fansapi:', 'synthetic': 'synthetic:'}.get(policy.transport_kind if policy else None)
    review = bool(rows and held(s, runtime.account_id, fan_id))
    for decision, intent in rows[:10]:
        try:
            terms = json.loads(decision.terms_json)
            grant = s.get(OfEngineOfferGrant, (runtime.account_id, decision.offer_grant_id))
            if (terms['item']['account_id'] != runtime.account_id
                    or terms['item']['id'] != decision.catalog_item_id or intent.catalog_item_id != decision.catalog_item_id
                    or terms['price_cents'] != intent.price_cents or terms['media_ids'] != json.loads(intent.media_json)
                    or terms['preview_ids'] != json.loads(intent.preview_json)
                    or grant is None or grant.binding_id != runtime.binding_id or grant.fan_id != fan_id
                    or grant.used_intent_id != intent.intent_id or grant.terms_hash != _hash(terms)):
                raise ValueError()
            label = terms['item'].get('label') or terms['item']['description_for_ai']
            if type(label) is not str:
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            complete = False
            continue
        card = {'itemId': str(decision.catalog_item_id), 'intentId': intent.intent_id,
            'label': label, 'priceCents': intent.price_cents, 'currency': 'USD',
            'sendState': intent.status, 'accessState': 'unconfirmed', 'financialState': 'not_established',
            'financialReview': review, 'messageId': None}
        frozen = terms.get('commercial')
        if isinstance(frozen, dict):
            # Product facts of the card exactly as frozen at preparation; a later terms revision cannot change them.
            card.update(kind=frozen['kind'], variant=frozen['variant'], counts=frozen['counts'])
        if intent.status == 'accepted':
            offer = s.get(ContentOffer, intent.offer_id) if intent.offer_id is not None else None
            message = s.get(Message, (runtime.account_id, fan_id, intent.provider_message_id)) if intent.provider_message_id else None
            if (offer is None or offer.account_id != runtime.account_id or offer.fan_id != fan_id
                    or offer.item_id != decision.catalog_item_id or offer.price_cents != intent.price_cents
                    or offer.offer_message_id != intent.provider_message_id or intent.projected_at is None
                    or message is None or message.direction != 'out'):
                complete = False
                continue
            card.update(messageId=str(intent.provider_message_id), offerId=str(offer.id), offerState=offer.status,
                        messageDeleted=bool(message.is_unsent))
            access = s.scalar(select(OfEngineAccessEvidence).where(
                OfEngineAccessEvidence.account_id == runtime.account_id, OfEngineAccessEvidence.fan_id == fan_id,
                OfEngineAccessEvidence.intent_id == intent.intent_id,
                OfEngineAccessEvidence.message_id == intent.provider_message_id,
                OfEngineAccessEvidence.applied.is_(True),
                OfEngineAccessEvidence.evidence_ref.like((prefix or 'unavailable:') + '%'))
                .order_by(OfEngineAccessEvidence.occurred_at.desc(), OfEngineAccessEvidence.evidence_key.desc()).limit(1))
            if access is not None:
                card.update(accessState=access.status, accessEvidence=access.evidence_key,
                            accessAt=access.occurred_at.isoformat())
        cards.append(card)
    return {'cards': cards, 'cardsCoverage': 'complete' if complete else 'incomplete', 'cardsTruncated': len(rows) > 10}


def _match(s, runtime, fan_id, text):
    rows = s.execute(select(ContentOffer, OfTextIntent, OfEngineDecision)
        .outerjoin(OfTextIntent, and_(OfTextIntent.account_id == ContentOffer.account_id,
                                     OfTextIntent.offer_id == ContentOffer.id))
        .outerjoin(OfEngineDecision, and_(OfEngineDecision.account_id == OfTextIntent.account_id,
                                         OfEngineDecision.intent_id == OfTextIntent.intent_id))
        .where(ContentOffer.account_id == runtime.account_id, ContentOffer.fan_id == fan_id,
               ContentOffer.status.in_(('open', 'unlocked')))
        .order_by(ContentOffer.id).limit(51)).all()
    if not rows:
        return {'state':'none', 'offers':[]}
    if len(rows) > 50:
        return {'state':'incomplete', 'offers':[]}
    candidates = []
    for offer, intent, decision in rows:
        if (intent is None or decision is None or decision.action != 'offer' or intent.status != 'accepted'
                or intent.binding_id != runtime.binding_id or decision.binding_id != runtime.binding_id
                or decision.fan_id != fan_id or intent.projected_at is None or not decision.terms_json):
            return {'state':'incomplete', 'offers':[]}
        message = s.get(Message, (runtime.account_id, fan_id, intent.provider_message_id)) if intent.provider_message_id else None
        if message is None or message.is_unsent:
            return {'state':'incomplete', 'offers':[]}
        try:
            terms = json.loads(decision.terms_json)
            if (terms['item']['id'] != offer.item_id or terms['price_cents'] != offer.price_cents
                    or terms['item']['account_id'] != runtime.account_id):
                return {'state':'incomplete', 'offers':[]}
            label = terms['item'].get('label') or terms['item']['description_for_ai']
            refs = [normalize(label), *aliases_for(s, runtime, offer.item_id, terms)]
        except (ValueError, TypeError, KeyError):
            return {'state':'incomplete', 'offers':[]}
        frozen = terms.get('commercial')
        candidates.append({'offerId':str(offer.id), 'itemId':str(offer.item_id), 'intentId':intent.intent_id,
            'label':label, 'priceCents':offer.price_cents, 'currency':'USD', 'references':refs,
            **({'kind': frozen['kind'], 'variant': frozen['variant'], 'counts': frozen['counts']} if isinstance(frozen, dict) else {})})
    normalized = normalize(text)
    named = [candidate for candidate in candidates if any(ref and (' ' + ref + ' ') in (' ' + normalized + ' ')
                                                           for ref in candidate['references'])]
    if len(named) == 1:
        selected, method = named[0], 'named'
    elif len(named) > 1:
        return {'state':'ambiguous', 'offers':[]}
    elif normalized in _GENERIC and len(candidates) == 1:
        selected, method = candidates[0], 'sole_open'
    elif normalized in _GENERIC and len(candidates) > 1:
        return {'state':'ambiguous', 'offers':[]}
    else:
        return {'state':'unclear', 'offers':[]}
    selected = {key:value for key,value in selected.items() if key != 'references'}
    return {'state':'matched', 'match':method, 'offers':[selected]}
