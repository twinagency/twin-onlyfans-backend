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
    # Preserve a supported prefix such as "outfit photo, no live claim".
    # A negated list remains one unit: "no toy, and feet" proves neither.
    result=[]
    for asset in product.get('assets',[]):
        for fact in asset.get('facts',[]):
            for sentence in re.split(r'[.;]',fact):
                prefix=re.split(r',\s*(?=(?:no|not|without|never)\b)',sentence,maxsplit=1,flags=re.I)[0]
                if not re.search(r"\b(?:no|not|without|never)\b",prefix,re.I):
                    result.append(prefix)
    return ' '.join(result)


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
    from .request_evidence import request_source, affirmative_requests
    source=request_source(snapshot,decision.get('request_quote'))
    request=' '.join(affirmative_requests(source['text'])) if source else ''
    rendered=' '.join(texts)
    # With no current request (e.g. acceptance of a retained invitation), check
    # the caption's own content claim. Old fan topics are never revived.
    wanted=topics(request) if request else topics(rendered)
    actual=set(_CATEGORY.get(product.get('category'),set()))|topics(_positive_facts(product))
    relevant=(not wanted or bool(wanted & actual)) if request else wanted<=actual
    terms=snapshot.get('inventory',{}).get(decision.get('product_id'),{})
    kinds={f.get('kind') for f in terms.get('files',[]) if f.get('kind')}
    format_source=request or rendered
    video=bool(re.search(r'\bvideos?\b',format_source,re.I))
    photo=bool(re.search(r'\b(?:photos?|pics?|pictures?)\b',format_source,re.I))
    format_mismatch=(video and not photo and kinds and 'video' not in kinds) or (photo and not video and kinds and not kinds & {'photo','image'})
    if relevant and not format_mismatch:return None
    # A real different alternative must say what is different. "Closest to
    # bare tits" does not turn a booty-only product into breast content.
    named_actual=topics(rendered) & actual
    named_format=(format_mismatch and bool(re.search(r'\b(?:photo|pic|picture)\b',rendered,re.I)) and kinds<={'photo','image'}) or (format_mismatch and bool(re.search(r'\bvideo\b',rendered,re.I)) and kinds=={'video'})
    if _ALT.search(rendered) and (named_actual or named_format):return None
    return 'product_request_mismatch'
