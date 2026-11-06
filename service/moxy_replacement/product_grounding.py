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


def _media_boundary_supported(snapshot,sentence,named):
    """A narrow category ban cannot become a broader topic/conversation ban."""
    prohibited=_prohibited(snapshot)
    from .request_evidence import request_source,affirmative_requests
    source=request_source(snapshot)
    request=source.get('text','') if source else ''
    explicit_media=bool(re.search(r'\b(?:photos?|pics?|pictures?|videos?|content|menu|catalogue|vault)\b',sentence,re.I))
    requested_media=bool(re.search(r'\b(?:photos?|pics?|pictures?|videos?|content)\b',request,re.I)
                         or any(re.search(r'\b(?:show|send)\b',r,re.I) for r in affirmative_requests(request)))
    if not explicit_media and not requested_media:return False
    covered=set()
    for category in prohibited:
        scope=_CATEGORY.get(category,{category})
        if category=='feet_photos' and not (re.search(r'\b(?:photos?|pics?|pictures?)\b',sentence,re.I)
                                           and not re.search(r'\bvideos?\b',sentence,re.I)):continue
        if category=='feet_photos_video' and not explicit_media:continue
        if category in {'nude_booty','covered_booty','bikini_lingerie_booty'}:
            qualifier=r'\bnud[ei]\w*\b|\bnaked\b' if category=='nude_booty' else r'\bcovered\b|\bbikini\b|\blingerie\b'
            if not re.search(qualifier,sentence,re.I):continue
        covered.update(scope)
    return bool(named and named<=covered)


def _card_claim_problem(snapshot,decision,texts):
    """Verify selected/referenced item claims independently of request relevance."""
    from .state import referenced_card,CARD_REFERENCE
    from .conversation import clauses,quotes
    for text in texts:
        for sentence in clauses(text):
            # Historical speech/retractions and negated attributes are not new
            # positive item facts. Split contrast clauses before testing scope.
            for claim in re.split(r'\bbut\b',sentence,flags=re.I):
                if re.search(r"\b(?:you (?:asked|wanted|said)|i (?:said|claimed|was wrong)|not|no|never|without|isn't|is not|don't|doesn't|can't|cannot)\b",claim,re.I):continue
                if claim.rstrip().endswith('?'):continue
                card=referenced_card(snapshot,claim,allow_current=decision.get('product_id') is None)
                key=card.get('product_id') if card else decision.get('product_id')
                product=snapshot.get('products',{}).get(key)
                if not product:
                    if CARD_REFERENCE.search(claim) and re.search(r'\b(?:photos?|pics?|videos?|minutes?|seconds?)\b',claim,re.I):
                        return 'unsupported_card_content_claim'
                    continue
                facts=_positive_facts(product)
                actual=set(_CATEGORY.get(product.get('category'),set()))|topics(facts)
                # A plain conversational topic is not an assertion about the
                # card. The card's own caption or an explicit card description is.
                item_claim=bool(re.search(r'\b(?:here|this|that|it|card|photo|pic|picture|video|clip|set|bundle)\b',claim,re.I))
                if not item_claim:continue
                if not topics(claim)<=actual:return 'unsupported_card_content_claim'
                terms=snapshot.get('inventory',{}).get(key,{})
                files=terms.get('files',[])
                counts={'photo':sum(f.get('kind') in {'photo','image'} for f in files),
                        'video':sum(f.get('kind')=='video' for f in files)}
                for kind,pattern in [('photo',r'\b(?:photos?|pictures?|pics?|images?)\b'),('video',r'\b(?:videos?|clips?)\b')]:
                    if re.search(pattern,claim,re.I) and files and counts[kind]==0:return 'unsupported_card_content_claim'
                    found=re.search(r'\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+'+pattern[2:],claim,re.I)
                    if found and files:
                        value=found.group(1).lower()
                        number=int(value) if value.isdigit() else {'one':1,'two':2,'three':3,'four':4,'five':5,'six':6,'seven':7,'eight':8,'nine':9,'ten':10}[value]
                        if counts[kind]!=number:return 'unsupported_card_content_claim'
                for duration in re.finditer(r'\b(\d+(?:\.\d+)?)\s*(?:-| )?\s*(minutes?|mins?|seconds?|secs?)\b',claim,re.I):
                    amount,unit=duration.groups()
                    stem='min' if unit.lower().startswith('min') else 'sec'
                    if not re.search(r'(?<![\d.])'+re.escape(amount)+r'\s*'+stem+r'(?:ute|ond)?s?\b',facts,re.I):
                        return 'unsupported_card_content_claim'
    return None


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
            # An ordinary trip/personal photo statement is not a catalogue
            # prohibition. Unqualified "I don't do photos" still is one.
            if not named and re.search(r'\b(?:from|of|at|on)\s+(?:that|this|my|your|the)\s+\w+',sentence,re.I):continue
            if not named and not re.search(r'\b(?:photos?|pictures?|pics?|videos?|content|menu|vault|catalogue|catalog)\b',sentence,re.I):continue
            # A genuine unconditional creator restriction remains valid. The
            # presence/absence of pilot inventory never creates such a rule.
            if _media_boundary_supported(snapshot,sentence,named):continue
            return 'unsupported_catalogue_prohibition'
    product=snapshot.get('products',{}).get(decision.get('product_id'))
    if not product:return _card_claim_problem(snapshot,decision,texts)
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
    if relevant and not format_mismatch:return _card_claim_problem(snapshot,decision,texts)
    # A real different alternative must say what is different. "Closest to
    # bare tits" does not turn a booty-only product into breast content.
    named_actual=topics(rendered) & actual
    named_format=(format_mismatch and bool(re.search(r'\b(?:photo|pic|picture)\b',rendered,re.I)) and kinds<={'photo','image'}) or (format_mismatch and bool(re.search(r'\bvideo\b',rendered,re.I)) and kinds=={'video'})
    if _ALT.search(rendered) and (named_actual or named_format):return _card_claim_problem(snapshot,decision,texts)
    return 'product_request_mismatch'
