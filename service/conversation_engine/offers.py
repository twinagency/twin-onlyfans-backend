"""Reference outstanding offers without treating a reference as payment."""
import json
from sqlalchemy import and_, select
from db.models import ContentOffer
from db.onlyfans_delivery_models import OfTextIntent
from db.onlyfans_engine_models import OfEngineDecision
from .catalogue import aliases_for, normalize

_GENERIC = frozenset({'yes', 'yes please', 'send it', 'that one', 'i want it', 'buy it', 'ill take it'})


def match(s, runtime, fan_id, text):
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
        try:
            terms = json.loads(decision.terms_json)
            if (terms['item']['id'] != offer.item_id or terms['price_cents'] != offer.price_cents
                    or terms['item']['account_id'] != runtime.account_id):
                return {'state':'incomplete', 'offers':[]}
            label = terms['item'].get('label') or terms['item']['description_for_ai']
            refs = [normalize(label), *aliases_for(s, runtime, offer.item_id, terms)]
        except (ValueError, TypeError, KeyError):
            return {'state':'incomplete', 'offers':[]}
        candidates.append({'offerId':str(offer.id), 'itemId':str(offer.item_id), 'intentId':intent.intent_id,
            'label':label, 'priceCents':offer.price_cents, 'currency':'USD', 'references':refs})
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
