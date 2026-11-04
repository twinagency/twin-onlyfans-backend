"""Bounded grounding checks for content relevance and creator-wide boundaries.

Asset identity, price, consent, ownership and eligibility remain runtime authority.
This screen handles explicit body/format requests and clear blanket availability
claims; it does not claim general semantic entailment of arbitrary captions.
"""
import re

_TOPIC={
 'breasts':re.compile(r"\b(?:breasts?|boobs?|tits?|nipples?|hand[ -]?bra)\b",re.I),
 'booty':re.compile(r"\b(?:ass|booty|butt(?:ocks)?|twerk(?:ing)?)\b",re.I),
 'feet':re.compile(r"\b(?:feet|foot|toes?)\b",re.I),
 'kitty':re.compile(r"\b(?:kitty|pussy|vulva)\b",re.I),
 'bootyhole':re.compile(r"\b(?:bootyhole|asshole|anus)\b",re.I),
 'xxx':re.compile(r"\bxxx\b",re.I),
}
_CATEGORY={'covered_boobs':{'breasts'},'hand_bra':{'breasts'},'nipples':{'breasts'},
 'covered_booty':{'booty'},'bikini_lingerie_booty':{'booty'},'nude_booty':{'booty'},
 'feet_photos':{'feet'},'feet_photos_video':{'feet'},'kitty':{'kitty'},'bootyhole':{'bootyhole'},'xxx':{'xxx'}}
_REQUEST=re.compile(r"\b(?:show|send|see|want|like|have|photo|pic(?:ture)?|video|worth)\b",re.I)
_ASSENT=re.compile(r"^(?:(?:ok(?:ay)?|sure|yes|yeah|yep|please|go ahead|send it|do it)[\s,!?.]*)+$",re.I)
_ALT=re.compile(r"\b(?:instead|different|alternative|rather than)\b",re.I)
_GLOBAL=re.compile(
 r"\b(?:i|we)\s+(?:don'?t|do not|never)\s+(?:do|make|sell|offer|show|send|have)\b"
 r"|\ball\s+my\b.{0,55}\b(?:photos?|pictures?|pics?|videos?|only)\b"
 r"|\bno\b(?:\s+\w+){0,4}\s+(?:videos?|photos?|pics?)\b"
 r"|\bno\b.{0,35}\b(?:on (?:my|the) menu|in (?:my|the) (?:vault|catalogue|catalog))\b"
 r"|\b(?:isn'?t|aren'?t|is not|are not)\s+(?:a thing|on (?:my|the) menu)\b",re.I)
_SCOPE=re.compile(r"\b(?:right now|at the moment|currently|mapped|pilot|this selection|can send now|on (?:that|this) (?:one|photo|item|card))\b",re.I)


def topics(text):
    return {name for name,pattern in _TOPIC.items() if pattern.search(text or '')}


def _positive_facts(product):
    # Negative description clauses cannot establish the presence of a feature.
    facts=' '.join(f for asset in product.get('assets',[]) for f in asset.get('facts',[]))
    return ' '.join(part for part in re.split(r'[.;]|,\s*(?:with|and)\s+',facts)
                    if not re.search(r"\b(?:no|not|without|never)\b",part,re.I))


def _request(snapshot):
    from .conversation import pending_messages
    pending=pending_messages(snapshot)
    for m in reversed(pending):
        text=m.get('text','')
        if topics(text) and _REQUEST.search(text):return text
    # An acceptance can refer back to the actual preceding request, but an
    # unrelated new conversation does not inherit an old product topic.
    last=pending[-1].get('text','') if pending else ''
    if not _ASSENT.fullmatch(last.strip()):return ''
    pending_ids={m['id'] for m in pending}
    for m in reversed(snapshot.get('history',[])):
        if m['direction']!='in' or m['id'] in pending_ids:continue
        text=m.get('text','')
        if topics(text) and _REQUEST.search(text):return text
        if not _ASSENT.fullmatch(text.strip()):break
    return ''


def _prohibited(snapshot):
    for rule in snapshot.get('instructions',{}).get('rules',[]):
        if rule.get('key')=='prohibited_categories' and not rule.get('requires'):
            return set(rule.get('value') or [])
    return set()


def _conditional_boundary(snapshot,sentence):
    # Named conditional source rules can justify their actual restriction. A
    # general missing pilot category cannot be promoted to an unconditional no.
    names={'product:nipples':r'\b(?:nipples?|nips?)\b'}
    for rule in snapshot.get('instructions',{}).get('rules',[]):
        if rule.get('key') not in names or not re.search(names[rule['key']],sentence,re.I):continue
        value=rule.get('value');required={**rule.get('requires',{}),**(value.get('requires',{}) if isinstance(value,dict) else {})}
        terms={'vip':r'\bvip\b','spender':r'\bspend(?:er|ing)\b','high_spender':r'\bhigh[- ]spend(?:er|ing)\b'}
        if required and all(k in terms and re.search(terms[k],sentence,re.I) for k in required):return True
    return False


def product_problem(snapshot,decision,texts):
    """Return a specific refusal reason, without selecting or repricing a product."""
    prohibited=_prohibited(snapshot)
    for text in texts:
        for sentence in re.split(r'[,.!?;\n]+',text.replace('’',"'")):
            global_claim=_GLOBAL.search(sentence)
            if not global_claim or _SCOPE.search(sentence):continue
            prefix=sentence[:global_claim.start()]
            if re.search(r"\b(?:wrong (?:to|when)|shouldn'?t have|should not have|mistake to|incorrect to|retract|take back)\b",prefix,re.I):continue
            if _conditional_boundary(snapshot,sentence):continue
            named=topics(sentence)
            if not named and not re.search(r'\b(?:photos?|pictures?|pics?|videos?|content|menu|vault|catalogue|catalog)\b',sentence,re.I):continue
            # A genuine unconditional creator restriction remains valid. The
            # presence/absence of pilot inventory never creates such a rule.
            if named and named<=prohibited:continue
            return 'unsupported_catalogue_prohibition'
    product=snapshot.get('products',{}).get(decision.get('product_id'))
    if not product:return None
    request=_request(snapshot)
    wanted=topics(request)
    actual=set(_CATEGORY.get(product.get('category'),set()))|topics(_positive_facts(product))
    relevant=not wanted or bool(wanted & actual)
    terms=snapshot.get('inventory',{}).get(decision.get('product_id'),{})
    kinds={f.get('kind') for f in terms.get('files',[]) if f.get('kind')}
    video=bool(re.search(r'\bvideos?\b',request,re.I))
    photo=bool(re.search(r'\b(?:photos?|pics?|pictures?)\b',request,re.I))
    format_mismatch=(video and not photo and kinds and 'video' not in kinds) or (photo and not video and kinds and not kinds & {'photo','image'})
    if relevant and not format_mismatch:return None
    rendered=' '.join(texts)
    # A real different alternative must say what is different. "Closest to
    # bare tits" does not turn a booty-only product into breast content.
    named_actual=topics(rendered) & actual
    named_format=(format_mismatch and bool(re.search(r'\b(?:photo|pic|picture)\b',rendered,re.I)) and kinds<={'photo','image'}) or (format_mismatch and bool(re.search(r'\bvideo\b',rendered,re.I)) and kinds=={'video'})
    if _ALT.search(rendered) and (named_actual or named_format):return None
    from .conversation import pending_messages
    pending=pending_messages(snapshot)
    invitation=snapshot.get('invitation') or {}
    if invitation.get('product_id')==decision.get('product_id') and pending and all(_ASSENT.fullmatch(m.get('text','').strip()) for m in pending):
        proposal=[]
        for m in reversed(snapshot.get('history',[])):
            if m['direction']=='in':
                if m['id'] in {x['id'] for x in pending}:continue
                break
            if m['direction']=='out':proposal.append(m.get('text',''))
        proposal=' '.join(reversed(proposal))
        if _ALT.search(proposal) and topics(proposal)&actual:return None
    return 'product_request_mismatch'
