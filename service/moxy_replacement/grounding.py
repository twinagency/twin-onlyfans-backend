"""Narrow guard for the unsupported work/routine claims seen in repair evidence.

This is not a general factuality classifier. Public facts can ground an exact
sentence; style references, fan statements and previous bot replies cannot.
Unrecognised paraphrases remain a model-review concern.
"""
import re

_CLAIM = re.compile(
    r"\bi\s+(?:(?:usually|always|often|mostly|normally|generally)\s+)?"
    r"(?:train|work\s+out|hit\s+the\s+gym|go\s+to\s+the\s+gym|coach|teach|film|"
    r"work\s+(?:as|at|in|for)|bounce\s+between)\b"
    r"|\bi(?:'m|\s+am)\s+(?:a|an)\s+(?:coach|trainer|instructor|teacher)\b"
    r"|(?:^|[,;]\s*|\band\s+|\bmy\s+)(?:mornings|evenings|weekdays)\s+(?:are|usually|mostly|normally)\b"
    r"|\bmy\s+day\s*job\b", re.I)


def _strings(value):
    if isinstance(value,str):yield value
    elif isinstance(value,dict):
        for child in value.values():yield from _strings(child)
    elif isinstance(value,list):
        for child in value:yield from _strings(child)


def _norm(text):
    return ' '.join(re.findall(r"[\w']+",text.replace('’',"'").lower()))


def unsupported_routine(snapshot,texts):
    facts=[_norm(f) for f in _strings(snapshot.get('public_facts',{}))]
    for text in texts:
        # Questions asking about the fan's routine do not make a creator claim.
        for sentence in re.split(r'[.!?;\n]+',text):
            if _CLAIM.search(sentence):
                claim=_norm(sentence)
                if not any(claim and claim in fact for fact in facts):return True
    return False


_GAZE=re.compile(r"\b(?:looking|gazing|staring)\s+(?:right\s+)?(?:back\s+)?(?:at|into)\s+(?:you|your\s+eyes|the\s+camera)\b",re.I)
_INVITE=re.compile(r"\b(?:want|wanna|would\s+you\s+like|shall\s+i|should\s+i)\b.{0,80}\b(?:send|see|show|look)\b",re.I)


def unsupported_asset_gaze(snapshot,decision,texts):
    product=snapshot.get('products',{}).get(decision.get('product_id'))
    if not product or not any(_GAZE.search(t) for t in texts):return False
    facts=' '.join(f for a in product['assets'] for f in a['facts'])
    for sentence in re.split(r'[.!?]+',facts):
        if (re.search(r"\b(?:look\w*|gaz\w*|star\w*|face)\b.{0,60}\bcamera\b",sentence,re.I)
                and not re.search(r"\b(?:not|never|no|away)\b",sentence,re.I)):return False
    return True


def unbound_product_invitation(snapshot,decision,texts):
    if decision['action']!='reply' or decision.get('product_id') is not None:return False
    existing={c.get('product_id') for c in snapshot.get('card_state',[])}
    existing.update(snapshot.get('fan_state',{}).get('purchased_products',[]))
    words=set(re.findall(r"[a-z]{4,}",' '.join(texts).lower()))
    for key,product in snapshot.get('products',{}).items():
        if key in existing or not re.search(r'(?<!\w)'+re.escape(key)+r'(?!\w)',decision.get('reason','')):continue
        # A proposal can be declarative ("yours when you say the word").
        # Existing-card discussion stays text-only. A fresh concrete invitation
        # must bind its identity; this validator never selects one for the model.
        facts=' '.join(f for a in product.get('assets',[]) for f in a.get('facts',[]))
        overlap=words & set(re.findall(r"[a-z]{4,}",facts.lower()))
        if any(_INVITE.search(t) for t in texts) or len(overlap)>=4:return True
    return False



def ignores_verified_ownership(snapshot,texts):
    state=snapshot.get('commercial_state') or {}
    card=state.get('last_offer') or {}
    if card.get('paid') is not True or card.get('product_id') not in state.get('verified_purchases',[]):return False
    incoming=(state.get('current_input') or {}).get('text','')
    # This targets the exact last-card ownership question, not a vague claim
    # about a different/earlier item or a fan's unverified report of payment.
    if not re.search(r"\bdo i (?:already )?own (?:it|that(?: exact)? one|this(?: exact)? one)\b",incoming,re.I):return False
    return any(re.search(r"\b(?:let me check|need to (?:check|verify)|not sure|can'?t confirm|don'?t know|before i say anything for sure)\b",t,re.I) for t in texts)
