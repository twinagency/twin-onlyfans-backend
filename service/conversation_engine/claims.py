"""Commercial-claim checks for a model proposal.

A plain reply may state grounded informational prices and scoped access facts.
Payment/ownership requires separately verified ownership; pitches still require
a verified offer action. The structured fields (`pitch`, `price_claims`,
`purchase_claim`) are the model's declaration; the text checks are a
defence-in-depth backstop for an undeclared claim.

The text checks are clause-level and structural, not bare word lists: a
negated, conditional, questioning or merely future-tense clause is not a claim
("No need to buy anything", "I will check your purchase"), a purchase statement
is accepted only when it names an item the fan verifiably owns, a price must
equal an approved price, and a content quantity must equal the payload (or a
fact already recorded in the conversation). Neither is a perfect classifier:
real-model grounding still has to be measured separately.

Product truth. A card's lead-in (an `offer` proposal, or an operator edit of one) is held to the ONE grant it selected, and
only to that grant: every asserted mention of a media noun states the card's kind, every stated count must be the grant's own
count of that kind, a duration needs a stated duration, an amount must be the grant's price. Another grant, an earlier card,
the catalogue and price listings authorize nothing for it. The model judge, the plan backstop and the operator edit all call
this one module.
"""
import re
from dataclasses import dataclass

from onlyfans_runtime.store import Refused


@dataclass(frozen=True)
class Grounding:
    """Facts the text may rely on. Defaults are strict (nothing is grounded)."""
    quantities: frozenset = frozenset()        # media counts that are real (offer payload, sent cards, catalogue match)
    purchase_labels: tuple = ()                # labels of items the fan verifiably owns
    media_reference: bool | None = None        # current request/reference may depend on media; never action authority
    current_offer_prices: frozenset = frozenset()  # informational replies only; frozen eligible terms
    access_labels: tuple = ()                  # accepted cards with confirmed access, not payment/ownership
    # Product wording (price without a currency, media type, duration). Without a product in the snapshot (a scoped
    # price/grant/card referent, or a request about content or its price) only draft clauses that name a product are checked.
    product_context: bool = False
    media_kinds: frozenset = frozenset()       # 'picture' / 'video' kinds the scoped referent(s) are known to contain
    durations: frozenset = frozenset()         # seconds; absent facts never authorize a duration
    # An offer card is attached to this text: its ONE selected authorized grant is the only authority for what the text may say
    # about the product (kind, count, duration, price). Nothing else in the snapshot (another grant, an earlier card, the
    # catalogue, a price listing) authorizes it. `media_quantities` is {(picture|video|other, count)}, zeros included.
    offer_bound: bool = False
    media_quantities: frozenset = frozenset()


def selected_offer(snapshot, proposal):
    """The exact authorized grant an offer proposal names, or None. Never a text-ranked neighbour, never a guess."""
    if proposal is None or getattr(proposal, 'action', 'reply') != 'offer':
        return None
    wanted = getattr(proposal, 'offer_grant_id', None)
    matches = [g for g in snapshot.get('offer_grants') or [] if isinstance(g, dict) and g.get('grantId') == wanted]
    return matches[0] if len(matches) == 1 else None


def _price_facts(grant):
    """Cents a text may state about this grant: its approved price, and the standard price only of an approved discount."""
    out = set()
    if grant.get('currency') == 'USD':
        if type(grant.get('priceCents')) is int and grant['priceCents'] > 0:
            out.add(grant['priceCents'])
        if grant.get('discountApproved') is True and type(grant.get('standardCents')) is int and grant['standardCents'] > 0:
            out.add(grant['standardCents'])
    return out


def grounding_from_snapshot(snapshot, proposal=None):
    """Build Grounding from a writer snapshot. `offer_grants`/`verified_purchases` exist only for a real model."""
    quantities, media_quantities = set(), set()
    offering = proposal is not None and getattr(proposal, 'action', 'reply') == 'offer'
    # A real-model snapshot carries its authorized grants; a synthetic fixture does not, and keeps the legacy (unbound) checks.
    bound = offering and 'offer_grants' in snapshot
    selected = selected_offer(snapshot, proposal) if bound else None
    grants = ([selected] if selected is not None else []) if bound else snapshot.get('offer_grants') or []
    for grant in grants:
        if type(grant.get('mediaCount')) is int and grant['mediaCount'] > 0:
            quantities.add(grant['mediaCount'])
        media = grant.get('media')
        if isinstance(media, dict):   # payload facts: total plus per-kind counts (the writer judge checks which noun)
            for key in ('total', 'pictures', 'videos'):
                if type(media.get(key)) is int and media[key] > 0:
                    quantities.add(media[key])
            for kind, key in (('picture', 'pictures'), ('video', 'videos'), ('other', 'total')):
                if type(media.get(key)) is int and media[key] >= 0:
                    media_quantities.add((kind, media[key]))
    for row in ([] if bound else snapshot.get('history') or []):
        if row.get('direction') == 'out' and type(row.get('media_count')) is int and row['media_count'] > 0:
            quantities.add(row['media_count'])
    for item in ([] if bound else (snapshot.get('catalogue_search') or {}).get('items') or []):
        if item.get('mediaIds'):
            quantities.add(len(item['mediaIds']))
    labels = tuple(str(p.get('label') or '') for p in (snapshot.get('verified_purchases') or []) if isinstance(p, dict))
    message = (snapshot.get('current_message') or {}).get('body')
    media_reference = None
    if isinstance(message, str):
        mentions = [(clause, match.start()) for clause, _ in _clauses(message.replace('’', "'"))
                    for match in re.finditer(r"\b" + _CONTENT_NOUN + r"\b", clause, re.I)]
        # A negated media request is not an evidence dependency. With no noun,
        # an already resolved card reference can ground contextual "it".
        media_reference = (any(not _negated(clause, start) for clause, start in mentions)
                           if mentions else bool((snapshot.get('offer_reference') or {}).get('offers')))
    if bound:
        prices = frozenset(_price_facts(selected)) if selected is not None else frozenset()
    else:
        prices = frozenset(g['priceCents'] for g in [*(snapshot.get('offer_grants') or []), *(snapshot.get('price_information') or [])]
                           if isinstance(g, dict) and type(g.get('priceCents')) is int
                           and g['priceCents'] > 0 and g.get('currency') == 'USD')
        # An approved discount card may also name the standard amount it is lower than; a standard card names only its own amount.
        prices |= frozenset(g['standardCents'] for g in grants if isinstance(g, dict) and g.get('discountApproved') is True
                            and type(g.get('standardCents')) is int and g['standardCents'] > 0 and g.get('currency') == 'USD')
    access_labels = tuple(card['label'] for card in (snapshot.get('offer_reference') or {}).get('cards') or []
                          if isinstance(card, dict) and isinstance(card.get('label'), str)
                          and card.get('sendState') == 'accepted' and card.get('accessState') == 'confirmed'
                          and card.get('financialReview') is False and card.get('messageDeleted') is not True)
    kinds, durations = set(), set()
    referents = [*[g for g in grants if isinstance(g, dict)],
                 *[p for p in snapshot.get('price_information') or [] if isinstance(p, dict)],
                 *[o for o in (snapshot.get('offer_reference') or {}).get('offers') or [] if isinstance(o, dict)],
                 *[c for c in (snapshot.get('offer_reference') or {}).get('cards') or [] if isinstance(c, dict)]]
    if offering:
        # The attached card is grounded by its selected grant only. Another
        # quoted/catalogue product cannot justify describing these photos as video.
        referents = [g for g in grants if isinstance(g, dict)]
    for ref in referents:
        media = ref.get('media') if isinstance(ref.get('media'), dict) else {}
        if type(media.get('pictures')) is int and media['pictures'] > 0:
            kinds.add('picture')
        if type(media.get('videos')) is int and media['videos'] > 0:
            kinds.add('video')
        if type(ref.get('durationSeconds')) is int and ref['durationSeconds'] > 0:
            durations.add(ref['durationSeconds'])
        for field in (() if offering else ('label', 'description')):
            if isinstance(ref.get(field), str):
                kinds.update(_kinds_in(ref[field]))
    from .writer_context import is_price_question
    product_context = bool(referents) or media_reference is True or is_price_question(message) or bound
    return Grounding(frozenset(quantities), labels, media_reference, prices, access_labels,
                     product_context, frozenset(kinds), frozenset(durations),
                     offer_bound=bound, media_quantities=frozenset(media_quantities))


_NUMBER_WORDS = {'zero': 0, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8,
                 'nine': 9, 'ten': 10, 'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14, 'fifteen': 15,
                 'sixteen': 16, 'seventeen': 17, 'eighteen': 18, 'nineteen': 19, 'twenty': 20, 'thirty': 30,
                 'forty': 40, 'fifty': 50, 'sixty': 60, 'seventy': 70, 'eighty': 80, 'ninety': 90,
                 'hundred': 100, 'dozen': 12}
_NUMBER_RX = r"(?:\d+|" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")"

_PRICE = re.compile(r"(?:[$£€]\s*\d|\b(?:usd|aud|eur|gbp|cad)\b\s*\$?\s*\d|\d\s*(?:usd|aud|eur|gbp|cad|dollars?|bucks|credits?|stars?)\b"
                    r"|\b" + _NUMBER_RX + r"[\s-]+(?:\w+[\s-]+)?(?:dollars?|bucks|credits?|stars?)\b)", re.I)
_AMOUNT = re.compile(r"(?:(?:[$£€]|\b(?:usd|aud|eur|gbp|cad)\b)\s*\$?\s*(\d+(?:[.,]\d{1,2})?)(?:\s*(?:usd|aud|eur|gbp|cad)\b)?"
                     r"|(\d+(?:[.,]\d{1,2})?)\s*(?:usd|aud|eur|gbp|cad|dollars?|bucks)\b)", re.I)
_WORD_AMOUNT = re.compile(r"\b(" + _NUMBER_RX + r")(?:[\s-]+(" + _NUMBER_RX + r"))?\s+(?:dollars?|bucks)\b", re.I)


def _cents(text):
    out = set()
    for match in _AMOUNT.finditer(text):
        raw = (match.group(1) or match.group(2)).replace(',', '.')
        whole, _, frac = raw.partition('.')
        out.add(int(whole) * 100 + int((frac + '00')[:2]))
    for match in _WORD_AMOUNT.finditer(text):
        values = [_NUMBER_WORDS.get(g.lower(), None) if not g.isdigit() else int(g) for g in match.groups() if g]
        out.add(sum(values) * 100)
    return out


# --- clause helpers ---------------------------------------------------------------------------------------------
_NEGATION = re.compile(r"\b(?:no|not|never|none|nothing|nobody|without|neither|nor|zero|hardly|"
                       r"(?:do|does|did|will|would|can|could|should|must|is|are|was|were|have|has|had|need|ought)n'?t|won'?t|can'?t|cannot|dont|cant|wont)\b"
                       r"|\bn'?t\b|\bno\s+need\b|\bno\s+pressure\b|\binstead\s+of\b|\brather\s+than\b", re.I)
_CONDITIONAL = re.compile(r"\b(?:if|whether|unless|in\s+case|once|until|supposing|assuming|when(?:ever)?)\b", re.I)
_FUTURE_CUE = re.compile(r"(?:\b(?:will|would|gonna|going\s+to|let\s+me|lemme|to|shall|ill)|'ll)\s*$", re.I)


def _clauses(text):
    """[(clause_text, is_question)]. Split on sentence ends and hard breaks; ',' and 'but/and' stay (negation is clause-local to the match)."""
    out = []
    for part in re.split(r"(?<=[.!?])\s+|\n+", text):
        part = part.strip()
        if part:
            out.append((part, part.rstrip().endswith('?')))
    return out


def _window(clause, start, words=6):
    """Up to `words` words immediately before a match, stopping at a clause break (comma, dash, semicolon, but)."""
    head = clause[:start]
    head = re.split(r"[,;:—–]|\bbut\b|\bhowever\b|\bthough\b|\bso\b", head)[-1]
    return ' '.join(head.split()[-words:])


def _negated(clause, start):
    return bool(_NEGATION.search(_window(clause, start)))


def _norm(value):
    return ' '.join(re.sub(r"[^a-z0-9]+", ' ', str(value).lower()).split())


# --- purchase / access claims -----------------------------------------------------------------------------------
_FINANCIAL_PATTERNS = (
    re.compile(r"\byou(?:'ve|\s+have|\s+had)?\s+(?:already\s+|just\s+|now\s+)?(?:purchased|bought|paid)\b", re.I),
    re.compile(r"\b(?:already|just)\s+(?:purchased|bought|paid\s+for)\b", re.I),
    re.compile(r"\bthank(?:s|\s+you)?\s+(?:so\s+much\s+)?for\s+(?:buying|purchasing|paying|the\s+purchase|your\s+(?:purchase|payment|order|support))\b", re.I),
    re.compile(r"\b(?:got|received|have|saw|see|seen|noticed|love|appreciate|confirmed?)\s+your\s+(?:purchase|payment|order)\b", re.I),
    re.compile(r"\byour\s+(?:purchase|payment|order)\s+(?:went|came|is|was|has|have|been|done|confirmed|complete|success|received|through)\b", re.I),
    # "you own me" / "you own my heart" is possessive banter, not a statement about an owned item.
    re.compile(r"\byou\s+(?:now\s+|already\s+)?own\b(?!\s+(?:me|us|my|this\s+(?:body|heart)|every(?:thing|one)\s+about\s+me)\b)", re.I),
)
_ACCESS_PATTERNS = (
    re.compile(r"\byou(?:'ve|\s+have|\s+had)?\s+(?:already\s+|just\s+|now\s+)?unlocked\b", re.I),
    re.compile(r"\b(?:already|just)\s+unlocked\b", re.I),
    re.compile(r"\bthank(?:s|\s+you)?\s+(?:so\s+much\s+)?for\s+unlocking\b", re.I),
    re.compile(r"\b(?:got|received|have|saw|see|seen|noticed|love|appreciate|confirmed?)\s+your\s+unlock\b", re.I),
    re.compile(r"\byour\s+unlock\s+(?:went|came|is|was|has|have|been|done|confirmed|complete|success|received|through)\b", re.I),
    re.compile(r"\b(?:it|that|this|they|these)(?:'s|\s+is|'re|\s+are)\s+(?:already\s+)?unlocked\b", re.I),
    re.compile(r"\byou\s+(?:now\s+|already\s+)?have\s+access\s+to\b", re.I),
)
_PURCHASE_PATTERNS = _FINANCIAL_PATTERNS + _ACCESS_PATTERNS


def _purchase_claims(text, patterns=_PURCHASE_PATTERNS):
    """Yield clauses that assert a purchase/payment/unlock/access as fact."""
    for clause, question in _clauses(text):
        if question:
            continue
        for rx in patterns:
            for match in rx.finditer(clause):
                window = _window(clause, match.start())
                if _NEGATION.search(window) or _CONDITIONAL.search(window) or _FUTURE_CUE.search(window):
                    continue
                # a future/inquiry cue directly before ("I will check ...") is not an assertion
                if re.search(r"\b(?:check|checking|look(?:ing)?\s+(?:at|into)|review|verify|confirm|find|track|sort|fix|ask\s+about)\b\s*$",
                             window, re.I) and re.match(r"your\b", match.group(0), re.I):
                    continue
                yield clause
                break
            else:
                continue
            break


# --- sales pitch ------------------------------------------------------------------------------------------------
_PITCH_PATTERNS = (
    re.compile(r"\b(?:buy|purchase|unlock|order)\b(?!ing)\s+(?:it|this|that|these|those|them|the|my|her|one|a|an|some|now|today|tonight|yours?|everything|all)\b", re.I),
    re.compile(r"\b(?:want|wanna|like|ready|going|gonna|should|can|could|would|will|let'?s|go\s+ahead\s+and|just|need)\s+(?:to\s+)?(?:buy|purchase|unlock|order)\b", re.I),
    re.compile(r"\bfor\s+sale\b|\bon\s+offer\b|\bspecial\s+offer\b|\bdiscount\b|\bon\s+sale\b|\bpromo(?:tion)?\b", re.I),
    re.compile(r"\bpay(?:ing)?\b(?!\s+(?:attention|off|back|me\s+a\s+compliment|closer))", re.I),
)


_PAST_INQUIRY = re.compile(r"\b(?:did|have|had|has|was|were)\s+(?:you|u|he|she|they)\b", re.I)


def _pitch_clauses(text):
    for clause, _question in _clauses(text):
        for rx in _PITCH_PATTERNS:
            if any(not _negated(clause, m.start()) and not _PAST_INQUIRY.search(_window(clause, m.start()))
                   for m in rx.finditer(clause)):
                yield clause
                break


# --- invented content quantities --------------------------------------------------------------------------------
_CONTENT_NOUN = r"(?:pics?|pictures?|photos?|photographs?|videos?|vids?|clips?|images?|selfies|snaps?|shots|reels?|gifs?|media|files|stills|sets)"
_FILLER = r"(?!(?:for|the|a|an|of|to|in|on|at|and|or|with|from|is|are|was|were|that|this|it)\b)"
# A count of content: a bare number (not part of a price such as "$5" or "6.00") then optional adjectives then a content noun.
_COUNT_NOUN = _CONTENT_NOUN[:-1] + r"|items?|pieces?|movies?|films?)"
_QUANTITY = re.compile(r"(?<![\d.,$\u00a3\u20ac])\b(" + _NUMBER_RX + r")(?:\s+(?:more|extra|new|other|different))?\s+(?:" + _FILLER + r"[a-z]+\s+){0,2}?" + _COUNT_NOUN + r"\b", re.I)


# Nouns that are content only sometimes: "2 shots of tequila", "3 sets at the gym". They count as a content quantity unless
# the same clause is clearly about something else (a drink, a workout). Unambiguous nouns (pics, videos, photos...) always count.
_AMBIGUOUS_NOUN = re.compile(r"^(?:shots|sets|snaps|reels|stills)$", re.I)
_NON_MEDIA_CONTEXT = re.compile(
    r"\b(?:gym|workout|work out|reps?|squats?|deadlifts?|lift(?:ing|s)?|cardio|exercis\w*|train(?:ing|ed)?|tennis|volleyball|"
    r"tequila|vodka|whisk(?:e)?y|rum|gin|beer|wine|champagne|jager\w*|espresso|coffee|drinks?|drank|drunk|margaritas?|bar|club|pub)\b", re.I)


def non_media_quantity(clause, noun):
    """True when `N <noun>` is an everyday count (drinks, gym sets), not a statement about content."""
    return bool(_AMBIGUOUS_NOUN.match(noun) and _NON_MEDIA_CONTEXT.search(clause))


def content_quantity_claims(text):
    """(count, kind) for every 'N <content noun>' statement (kind: picture|video|other).

    The ONE count grammar: the model judge, the plan backstop and the operator edit all read it, so they cannot disagree."""
    for clause, _question in _clauses(text.replace('\u2019', "'")):
        for match in _QUANTITY.finditer(clause):
            noun = match.group(0).split()[-1]
            if non_media_quantity(clause, noun):
                continue
            raw = match.group(1).lower()
            number = int(raw) if raw.isdigit() else _NUMBER_WORDS[raw]
            # "one" is the ordinary pronoun/determiner far more often than a count; only digits or a noun right after it count
            if raw == 'one' and not re.match(r"one\s+(?:" + _FILLER + r"\w+\s+)?" + _COUNT_NOUN + r"\b", match.group(0), re.I):
                continue
            if re.search(r"\b(?:minutes?|mins?|seconds?|secs?|hours?|hrs?|days?|weeks?|months?|years?)\b", match.group(0), re.I):
                continue   # a duration ("a 10 minute video") is not a count of items
            if _negated(clause, match.start()):
                continue
            yield number, _kind_of(noun)


def _quantity_claims(text):
    for number, _kind in content_quantity_claims(text):
        yield number


# --- product wording: price without a currency, media type and duration ----------------------------------------
# These recognise an assertion ABOUT THE PRODUCT being discussed ("the photo costs four", "it is a ten-minute video"),
# structurally (subject + verb + value), not a bare number or a bare word like "video". They run on the whole draft when
# the snapshot shows a product under discussion, otherwise on the clauses that name a product, and the referent's facts (price, kinds, duration) come only from the scoped
# snapshot: nothing here supplies a missing fact. A negated, quoted, attributed, conditional or questioning clause is
# not a claim.
_KIND_WORDS = {'picture': r"(?:pics?|pictures?|photos?|photographs?|images?|selfies|stills?)",
               'video': r"(?:videos?|vids?|clips?|reels?|gifs?|movies?|films?|footage)"}
_ANY_KIND = '(?:' + '|'.join(_KIND_WORDS.values()) + ')'
_WORD_ALT = '|'.join(sorted(_NUMBER_WORDS, key=len, reverse=True))
_NUM = r"(\d+(?:[.,]\d{1,2})?|(?:" + _WORD_ALT + r")(?:[\s-]+(?:" + _WORD_ALT + r"))?)"
_TIME_UNIT = r"(?:minutes?|mins?|seconds?|secs?|hours?|hrs?)"
_NOT_A_PRICE = (r"(?![\d.,]*\d)(?!\s*(?:%|percent|times?|more|of\b|" + _TIME_UNIT + r"|days?|weeks?|months?|years?|"
                r"dollars?|bucks|usd|aud|eur|gbp|cad|credits?|stars?|" + _ANY_KIND + r"\b))")
_PRICE_VERB = r"(?:costs?|costing|priced|prices?|charges?|charged|pays?|paying|goes\s+for|going\s+for|sells?\s+for|sold\s+for)"
_BARE_PRICES = (
    re.compile(r"\b" + _PRICE_VERB + r"\b(?:\s+(?:is|was|at|of|just|only|about|around|like|you|me))*\s+" + _NUM + r"\b" + _NOT_A_PRICE, re.I),
    re.compile(r"\b" + _ANY_KIND + r"\s+(?:is|are|was|were)\s+(?:only\s+|just\s+)?" + _NUM + r"\b" + _NOT_A_PRICE, re.I),
    re.compile(r"\b" + _NUM + r"\s+(?:for|per)\s+(?:the|a|an|this|that|each|one|it|them|both|all)\b", re.I),
)
_ATTRIBUTED = re.compile(r"\b(?:you|u|he|she|they)\s+(?:said|say|says|mentioned|wrote|thought|think|thinks|heard|told\s+me|asked|read|saw|claimed)\b"
                         r"|\b(?:said|says|saying|quote|quoted)\b", re.I)
_HYPOTHETICAL = re.compile(r"\b(?:imagine|suppose|pretend|hypothetical(?:ly)?|wish)\b|\b(?:if|as\s+if|as\s+though)\b[^.!?]*\bwere\b", re.I)
_PRODUCT_SUBJECT = re.compile(r"\b(?:it|this|that|these|those|they|one|prices?)\b|\b" + _ANY_KIND + r"\b", re.I)
# What a text says about the product is read structurally, not from a list of sentence frames: every asserted mention of a
# media noun describes the product it is attached to, wherever it sits in the sentence (copula, imperative, passive, verbless
# noun phrase, presentation). On an offer card (`offer_bound`) that is every mention. Without a card, a mention counts when it is
# visibly about the discussed/attached item (a demonstrative or "your" in its noun phrase, a copula whose subject is the item,
# "here ...", or a recipient: "for you", "you a ..."), so "I took a photo of my coffee" stays ordinary conversation.
_MEDIA_NOUN = re.compile(r"\b(" + _ANY_KIND + r")\b", re.I)
_NON_PRODUCT_COMPOUND = re.compile(r"^\s+(?:calls?|chats?|games?|shoots?|tapes?)\b", re.I)
_DEICTIC = re.compile(r"\b(?:this|that|these|those|your|attached|enclosed)\b", re.I)
_HERE = re.compile(r"\bhere(?:'s|\s+is|\s+are|\s+you\s+go)?\b", re.I)
_ITEM_COPULA = re.compile(r"\b(?:it|this|that|they|these|those)(?:'s|'re|\s+(?:is|are|was|were|will\s+be))\b", re.I)
_RECIPIENT_BEFORE = re.compile(r"\b(?:you|u)\s+(?:an?|some)\s+(?:[\w-]+\s+){0,3}$", re.I)
_RECIPIENT_AFTER = re.compile(r"^[^,;.!?]{0,40}?\b(?:for|to)\s+(?:you|u)\b", re.I)

_DURATION_CLAIMS = (
    # copula: a length needs "long" or a media noun after it ("it is a ten minute drive" is not a claim about the product)
    re.compile(r"\b(?:it|this|that|they|these|those)(?:'s|'re|\s+is|\s+are|\s+was|\s+will\s+be)\s+(?:just\s+|only\s+|actually\s+|about\s+|around\s+)?"
               r"(?:an?\s+)?" + _NUM + r"[\s-]*(" + _TIME_UNIT + r")(?:[\s-]*long\b|[\s-]+" + _ANY_KIND + r"\b)", re.I),
    re.compile(r"\b(?:it|this|that|they|these|those)\s+(?:runs?|lasts?|goes\s+for)\s+(?:just\s+|only\s+|about\s+|around\s+|roughly\s+)*"
               r"(?:an?\s+)?" + _NUM + r"[\s-]*(" + _TIME_UNIT + r")\b", re.I),
    re.compile(r"\b" + _ANY_KIND + r"\s+(?:is|are|was|runs?|lasts?|goes\s+for)\s+(?:just\s+|only\s+|about\s+|around\s+|roughly\s+)*"
               r"(?:an?\s+)?" + _NUM + r"[\s-]*(" + _TIME_UNIT + r")\b", re.I),
)


def _kinds_in(text):
    return {kind for kind, rx in _KIND_WORDS.items() if re.search(r"\b" + rx + r"\b", text, re.I)}


def _kind_of(noun):
    return next((kind for kind, rx in _KIND_WORDS.items() if re.fullmatch(rx, noun, re.I)), 'other')


def _number_value(raw):
    raw = raw.lower().replace(',', '.')
    if raw[0].isdigit():
        whole, _, frac = raw.partition('.')
        return int(whole) * 100 + int((frac + '00')[:2])
    return sum(_NUMBER_WORDS[w] for w in re.split(r"[\s-]+", raw) if w in _NUMBER_WORDS) * 100


def _scoped_negation(clause, start):
    """True when a negation in the window before `start` governs this noun phrase. A demonstrative or "your" after the
    negation re-anchors the phrase on the attached item ("don't miss this video" still says it is a video)."""
    window = _window(clause, start)
    last = None
    for match in _NEGATION.finditer(window):
        last = match
    return last is not None and not _DEICTIC.search(window[last.end():])


def _asserted(clause, question, start, *, anchored=False):
    """True when the match at `start` is the writer asserting it (not a question, negation, condition, quote or report).

    `anchored` (product nouns): negation must scope over the noun phrase itself, not merely occur earlier in the clause."""
    if question or _HYPOTHETICAL.search(clause) or (_scoped_negation(clause, start) if anchored else _negated(clause, start)):
        return False
    window = _window(clause, start, 8)
    if _CONDITIONAL.search(window) or _ATTRIBUTED.search(window) or clause[:start].count('"') % 2 or clause[:start].count('\u201c') > clause[:start].count('\u201d'):
        return False
    return True


def _bare_price_claims(text):
    """Cents asserted as the product's price without a currency word or sign ("the photo costs four")."""
    for clause, question in _clauses(text.replace('\u2019', "'")):
        if not _PRODUCT_SUBJECT.search(clause):
            continue
        for rx in _BARE_PRICES:
            for match in rx.finditer(clause):
                if rx is _BARE_PRICES[2] and not re.search(r"\b" + _ANY_KIND + r"\b", clause, re.I):
                    continue
                if _asserted(clause, question, match.start()):
                    yield _number_value(match.group(1))


def _directed(clause, start, end):
    """True when the media noun at clause[start:end] is visibly about the attached or discussed item."""
    words = clause[:start].split()
    head, near = ' '.join(words[-6:]), ' '.join(words[-3:])
    return bool(_DEICTIC.search(near) or _HERE.search(head) or _ITEM_COPULA.search(head)
                or _RECIPIENT_BEFORE.search(head + ' ') or _RECIPIENT_AFTER.match(clause[end:]))


_PREMODIFIER_DURATION = re.compile(r"(?:^|[^\w.])" + _NUM + r"[\s-]*(" + _TIME_UNIT + r")(?:[\s-]*long)?(?:\s+[a-z-]+){0,2}\s*$", re.I)


def _product_mentions(text, bound):
    """(kind, seconds | None) for every asserted mention of a product media noun that describes the item in question.
    `seconds` is a duration stated in the same noun phrase ("a ten-minute video")."""
    for clause, question in _clauses(text.replace('\u2019', "'")):
        for match in _MEDIA_NOUN.finditer(clause):
            if _NON_PRODUCT_COMPOUND.match(clause[match.end():]) or not _asserted(clause, question, match.start(), anchored=True):
                continue
            if not bound and not _directed(clause, match.start(), match.end()):
                continue
            premodifier = _PREMODIFIER_DURATION.search(clause[:match.start()])
            yield _kind_of(match.group(1)), (_seconds(premodifier) if premodifier else None)


def _seconds(match):
    value = _number_value(match.group(1)) / 100
    value *= {'s': 1, 'm': 60, 'h': 3600}[match.group(2)[0].lower()]
    return int(value) if value == int(value) else value


def _media_type_claims(text, bound=False):
    for kind, _seconds_claimed in _product_mentions(text, bound):
        yield kind


def _duration_claims(text, bound=False):
    for clause, question in _clauses(text.replace('\u2019', "'")):
        for rx in _DURATION_CLAIMS:
            for match in rx.finditer(clause):
                if not _NEGATION.search(match.group(0)) and _asserted(clause, question, match.start()):
                    yield _seconds(match)
    for _kind, seconds in _product_mentions(text, bound):
        if seconds is not None:
            yield seconds


# The draft itself can introduce a product ("the photo costs four", "it is a ten-minute video") when the fan only said hello.
# With no product in the snapshot, only a clause that itself names a product (a media noun or "price") is examined, so
# ordinary numbers and "it costs 4 to park" stay untouched; the facts still come only from the scoped snapshot, so with no
# evidence nothing supports the assertion.
_DRAFT_PRODUCT = re.compile(r"\b" + _ANY_KIND + r"\b|\bprices?\b", re.I)


def _product_clauses(text):
    return [clause for clause, _ in _clauses(text.replace('\u2019', "'")) if _DRAFT_PRODUCT.search(clause)]


def _check_product_wording(texts, grounding, verified_prices):
    if not grounding.product_context:
        texts = [clause for text in texts for clause in _product_clauses(text)]
    for text in texts:
        if any(cents not in verified_prices for cents in _bare_price_claims(text)):
            raise Refused('unsupported price claim in model text')
        if any(kind not in grounding.media_kinds for kind in _media_type_claims(text, grounding.offer_bound)):
            raise Refused('unsupported media type claim in model text')
        if any(seconds not in grounding.durations for seconds in _duration_claims(text, grounding.offer_bound)):
            raise Refused('unsupported media duration claim in model text')


# --- customs and role/identity claims ---------------------------------------------------------------------------
_CUSTOM_TERM = re.compile(r"\bcustoms?\b|\bcustom[\s-]+(?:made|video|vid|pic|pics|photo|photos|clip|clips|content|set|request|order)\b"
                          r"|\b(?:personali[sz]ed|made[\s-]to[\s-]order)\s+(?:video|vid|pic|pics|photo|photos|clip|clips|content|set)\b"
                          r"|\b(?:film|record|make|shoot|do|send)\s+(?:you\s+)?(?:a\s+|an\s+|some\s+)?(?:special|personal)\s+(?:video|vid|pic|pics|photo|photos|clip|clips|set)\b", re.I)
_COMMITMENT = re.compile(r"\b(?:i'?ll|i\s+will|i\s+can|i\s+could|i'?d|let\s+me|happy\s+to|gladly|sure|yes|yeah|yep|of\s+course|definitely|absolutely|"
                         r"got\s+you|can\s+do|i\s+do|i\s+offer|i\s+make|i\s+film|i\s+record|we\s+can|we\s+could|we'?ll)\b", re.I)


def _custom_promises(text):
    for clause, question in _clauses(text):
        if question:
            continue
        for match in _CUSTOM_TERM.finditer(clause):
            if _negated(clause, match.start()) or _CONDITIONAL.search(_window(clause, match.start(), 8)):
                continue
            commits = [m for m in _COMMITMENT.finditer(clause) if not _negated(clause, m.start())]
            if commits:
                yield clause
                break


_SUBJECT = re.compile(r"\b(?:i|she|he|the\s+creator|the\s+model|the\s+girl|herself|himself|myself)\b", re.I)
_REVIEW_VERB = re.compile(r"\b(?:read|reads|check|checks|review|reviews|reply|replies|respond|responds|answer|answers|type|types|"
                          r"write|writes|approve|approves|oversee|oversees|supervise|supervises|sees|see)\b", re.I)
_REVIEW_SCOPE = re.compile(r"\b(?:personally|myself|herself|himself)\b|"
                           r"\b(?:all|every|each|every\s+single|all\s+of)\s+(?:of\s+)?(?:the\s+|your\s+|these\s+|my\s+|her\s+)?"
                           r"(?:messages?|msgs?|dms?|texts?|repl(?:y|ies)|chats?|responses?)\b", re.I)
_INVOLVEMENT = (
    re.compile(r"\b(?:i|she|he|the\s+creator|they)\s+(?:personally|myself|herself|himself|themselves)\b", re.I),
    re.compile(r"\b(?:i'?m|i\s+am|this\s+is)\s+(?:actually\s+|really\s+|literally\s+|the\s+real\s+)(?:her|him|the\s+creator|the\s+model|the\s+girl)\b", re.I),
    re.compile(r"\b(?:she|he|the\s+creator)\s+(?:trained|built|made|programmed|wrote|designed|taught|created)\s+me\b|"
               r"\bi\s+was\s+(?:trained|built|made|programmed|designed|taught|created)\s+by\s+(?:her|him|the\s+creator|them)\b|"
               r"\bmy\s+(?:creator|owner)\s+(?:is|will|reads|reviews|approves|checks)\b", re.I),
    re.compile(r"\b(?:no\s+one|nobody)\s+else\s+(?:reads|replies|texts|types|answers|sees)\b", re.I),
)


def _involvement_claims(text):
    """Unsupported statements about who writes, reads or reviews the messages (creator involvement / identity)."""
    for clause, question in _clauses(text):
        if question:
            continue
        hit = any(not _negated(clause, m.start()) or re.search(r"\b(?:no\s+one|nobody)\b", m.group(0), re.I)
                  for rx in _INVOLVEMENT for m in rx.finditer(clause))
        if not hit:
            # subject + review verb + (personally | every message), all in one clause and not negated
            verb = _REVIEW_VERB.search(clause)
            subject = _SUBJECT.search(clause)
            scope = _REVIEW_SCOPE.search(clause)
            # The scope word must come with the review verb ("I read them myself", "I check every message"), not be an
            # unrelated reflexive that happens to precede it ("I touch myself when I see your messages").
            hit = bool(verb and subject and scope and not _negated(clause, verb.start()) and subject.start() < scope.end()
                       and (scope.start() > verb.start() or not re.fullmatch(r"(?:myself|herself|himself)", scope.group(0), re.I)))
        if hit:
            yield clause


def _check_quantities(texts, grounding):
    if grounding.offer_bound:
        # The text rides on one card: each stated count must be that grant's own count of that kind (zeros included).
        if any((kind, count) not in grounding.media_quantities for text in texts for count, kind in content_quantity_claims(text)):
            raise Refused('content quantity not supported by the selected offer payload')
        return
    numbers = [number for text in texts for number in _quantity_claims(text)]
    if numbers and not (set(numbers) <= grounding.quantities or (len(numbers) > 1 and sum(numbers) in grounding.quantities)):
        raise Refused('content quantity not supported by the payload')


# An inventory fact is not a source of conversational permission. Check the
# explicit causal claim, not a blanket refusal vocabulary. Truthful media
# unavailability and creator/privacy restrictions remain separate checks.
_CHAT_DENIAL = re.compile(r"\b(?:i|we)\s+(?:cannot|can not|can't|won't|do not|don't|are unable to|am unable to)\s+(?:discuss|talk(?:\s+about)?|chat(?:\s+about)?)\b", re.I)
_MEDIA_ABSENCE = re.compile(
    r"\b(?:no|without|missing|don't have|do not have|haven't got)\b[^.!?,]{0,60}\b(?:photos?|pictures?|pics?|videos?|clips?|media|content)\b"
    r"|\b(?:photos?|pictures?|pics?|videos?|clips?|media|content)\b[^.!?,]{0,30}\b(?:unavailable|not available|aren't available|are not available)\b", re.I)


def _inventory_conversation_claim(text):
    for clause, question in _clauses(text.replace('’', "'")):
        if question:
            continue
        for link in re.finditer(r"\b(because|since|so|means)\b", clause, re.I):
            before, after = clause[:link.start()], clause[link.end():]
            if link[1].lower() == 'because':
                if _CHAT_DENIAL.search(before) and _MEDIA_ABSENCE.search(after):
                    return True
            elif link[1].lower() == 'since':
                reason, comma, result = after.partition(',')
                if comma and _MEDIA_ABSENCE.search(reason) and _CHAT_DENIAL.search(result):
                    return True
            elif _MEDIA_ABSENCE.search(before) and _CHAT_DENIAL.search(after):
                return True
    return False


def check_plain_text(texts, proposal, sales_mode, verified_prices=frozenset(), grounding=None):
    """Refuse unsupported commercial content in model-written text.

    `sales_mode` is the effective mode after this proposal's own decline/reopen
    signal. `verified_prices` are cents from an already-sent offer that the
    engine matched for this message; only those may be restated in prose.
    `grounding` (optional, strict by default) lists payload quantities and
    verified-purchase labels and current media-reference context. Media absence
    cannot establish a general conversation prohibition, but may truthfully
    limit analysis of a particular requested item. Factual answers that carry
    none of these claims are untouched.
    """
    grounding = grounding or Grounding()
    if proposal.action == 'reply':
        verified_prices = set(verified_prices) | set(grounding.current_offer_prices)
    elif grounding.offer_bound:
        # Another card's price, an informational listing or an older offer never authorizes an amount in this card's lead-in.
        verified_prices = set(grounding.current_offer_prices)
    labels = [label for label in (_norm(l) for l in grounding.purchase_labels) if len(label) >= 3]
    access_labels = labels + [label for label in (_norm(l) for l in grounding.access_labels) if len(label) >= 3]
    if any(cents not in verified_prices for cents in proposal.price_claims):
        raise Refused('unsupported price claim in model text')
    if proposal.purchase_claim and not any(label in _norm(text) for label in labels for text in texts):
        raise Refused('unsupported purchase claim in model text')
    if proposal.pitch and proposal.action == 'reply':
        raise Refused('a pitch must use a verified offer')
    if proposal.pitch and sales_mode == 'paused':
        raise Refused('sales are paused')
    # A promised custom is the more specific refusal; it is judged before the product facts of the card it rides on.
    if any(next(_custom_promises(text), None) is not None for text in texts):
        raise Refused('promises a custom that is not supported')
    _check_quantities(texts, grounding)
    _check_product_wording(texts, grounding, set(verified_prices))
    for text in texts:
        if grounding.media_reference is False and _inventory_conversation_claim(text):
            raise Refused('media availability does not determine conversation permission; follow the documented creator limits')
        if _PRICE.search(text) and not (_cents(text) and _cents(text) <= set(verified_prices)
                                        and not re.search(r"\b(?:credits?|stars?)\b", text, re.I)
                                        and not any(re.search(r"[£€]|\b(?:aud|eur|gbp|cad)\b", amount.group(0), re.I)
                                                    for amount in _AMOUNT.finditer(text))):
            raise Refused('unsupported price claim in model text')
        for clause in _purchase_claims(text):
            # Access evidence never proves payment or ownership, including a
            # mixed clause containing both access and a financial assertion.
            supported = labels if next(_purchase_claims(clause, _FINANCIAL_PATTERNS), None) else access_labels
            if not any(label in _norm(clause) for label in supported):
                raise Refused('unsupported purchase claim in model text')
        if sales_mode == 'paused' and next(_pitch_clauses(text), None) is not None:
            raise Refused('sales are paused')
        if proposal.action == 'reply' and next(_pitch_clauses(text), None) is not None:
            raise Refused('a pitch must use a verified offer')
        if next(_involvement_claims(text), None) is not None:
            raise Refused('unsupported claim about who writes or reviews messages')


# --- creator background and current-time claims (U1b) -------------------------------------------------------------------
# A first-person statement about her job, pets, siblings, education, nationality, or the current local time is accepted only
# when her approved profile documents it (the time of day is never documented: the engine has no clock for her place).
# An absent fact is a gap, never a negative and never permission to invent one; a documented topic with a different detail
# (a nurse when the job is barista, three brothers when two are documented) is a contradiction. Same clause rules as the
# product wording: questions, negations, conditionals, quotes, attributions and hypotheticals are not claims, and the fan's
# own facts reflected back to him ("you work as a nurse?") are about him, not her. Unmatched wording is not a claim: this
# is a backstop for the common phrasings, like the age and city checks beside it.
_FACT_STOP = frozenset({'a', 'an', 'the', 'of', 'at', 'in', 'on', 'to', 'and', 'or', 'with', 'from', 'my', 'our', 'named', 'called',
                        'for', 'as', 'is', 'are', 'was', 'i', 'one', 'uni', 'university', 'college', 'school', 'older', 'younger',
                        'big', 'little', 'elder', 'currently', 'also', 'very', 'really', 'just'})
_FACT_SYNONYM = {'puppy': 'dog', 'puppie': 'dog', 'pup': 'dog', 'kitten': 'cat', 'kitty': 'cat', 'bunny': 'rabbit', 'bunnie': 'rabbit',
                 'pony': 'horse', 'ponie': 'horse'}
_CLAIM_CUT = frozenset({'and', 'but', 'so', 'because', 'then', 'now', 'before', 'when', 'while', 'until', 'which', 'that', 'though',
                        'lol', 'haha', 'babe', 'baby', 'since', 'after', 'if', 'or'})
_JOB_CUT = _CLAIM_CUT | {'at', 'in', 'for', 'with', 'during', 'from', 'every', 'on', 'by', 'as', 'i', 'part', 'full'}


def _fact_tokens(text, drop_one=False):
    out = set()
    for word in re.findall(r"[a-z0-9]+", str(text).lower().replace('’', "'")):
        if word in _FACT_STOP:
            continue
        if word in _NUMBER_WORDS:
            word = str(_NUMBER_WORDS[word])
        elif len(word) > 3 and word.endswith('s') and not word.endswith('ss'):
            word = word[:-1]
        word = _FACT_SYNONYM.get(word, word)
        if drop_one and word == '1':
            continue
        out.add(word)
    return out


def _phrase(raw, cut):
    words = []
    for word in (raw or '').lower().split():
        if word.strip(".,!?;:'\"") in cut:
            break
        words.append(word)
    return ' '.join(words)


_OWN = re.compile(r"(?:\bi\s+(?:just\s+|really\s+|also\s+)?(?:have|got|own|adopted|grew\s+up\s+with|live\s+with)|\bi'?ve(?:\s+got)?|\bi\s+have\s+got|\bmy)"
                  r"\s+((?:[\w'-]+\s+){0,3})$", re.I)
_OWN_BLOCK = frozenset({'to', 'about', 'for', 'of', 'from', 'thing', 'lot', 'against', 'without', 'into', 'not'})
_PET_NOUN = re.compile(r"\b(dogs?|puppy|puppies|pup|cats?|kittens?|kitty|rabbits?|bunny|bunnies|hamsters?|parrots?|guinea\s+pigs?|"
                       r"ferrets?|turtles?|tortoises?|ponies|pony)\b", re.I)
_SIBLING_NOUN = re.compile(r"\b(brothers?|sisters?|siblings?|twins?)\b(?!\s*-?\s*in-law)", re.I)
_ONLY_CHILD = re.compile(r"\bi(?:'m|\s+am)\s+an?\s+(only\s+child)\b", re.I)
_NAMED = re.compile(r"^\s*(?:named|called)\s+([a-z][\w'-]*)", re.I)

_EDU_NOT_YET = r"(?!\s*(?:hard|all|late|for|up|so|too|a\s+lot|really|tonight|today|now|until|at\s+the\s+moment)\b)"
_EDU_KEYS = r"\b(?:educat|degree|studi|college|school|uni(?:versity)?\b)"
_EDU_CLAIMS = (
    re.compile(r"\bi\s+(?:also\s+|just\s+|actually\s+)?(?:studied|majored\s+in|graduated\s+(?:from|with|in)|dropped\s+out\s+of|went\s+to\s+(?:uni|university|college)|"
               r"(?:have|got|hold|earned)\s+(?:an?|my)\s+(?:[a-z]+\s+)?degree(?:\s+in)?)\b" + _EDU_NOT_YET + r"\s*((?:[\w'-]+\s*){0,5})", re.I),
    re.compile(r"\bi(?:'m|\s+am)\s+(?:currently\s+)?(?:an?\s+(?:uni\s+|university\s+|college\s+)?student|studying)\b" + _EDU_NOT_YET
               + r"\s*((?:[\w'-]+\s*){0,5})", re.I),
)

_JOB_LEX = (r"(?:nurse|teacher|doctor|waitress|waiter|bartender|barista|lawyer|engineer|dancer|receptionist|accountant|cleaner|"
            r"hairdresser|stylist|makeup\s+artist|personal\s+trainer|photographer|designer|nanny|secretary|chef|pilot|"
            r"flight\s+attendant|paramedic|vet|pharmacist|therapist|dentist|cashier|florist|journalist|scientist|librarian|"
            r"beautician|masseuse|model|actress|dental\s+nurse|physio(?:therapist)?)")
_NOT_A_JOB = r"(?!(?:an?\s+|the\s+)?(?:hard|much|long|many|fast|well|late|early|little|soon|often|usual|always|if|though|team|you|your|my|his|her|good|best|hot)\b)"
_JOB_CLAIMS = (
    re.compile(r"\bi\s+(?:work|worked|used\s+to\s+work)\s+as\s+" + _NOT_A_JOB + r"(?:an?\s+|the\s+)?((?:[\w'-]+\s*){1,4})", re.I),
    re.compile(r"\bmy\s+(?:job|occupation|profession|career)\s+is\s+" + _NOT_A_JOB + r"(?:being\s+)?(?:an?\s+|the\s+)?((?:[\w'-]+\s*){1,4})", re.I),
    re.compile(r"\bi(?:'m|\s+am)\s+(?:currently\s+|actually\s+|also\s+|now\s+)?(?:an?\s+)?(" + _JOB_LEX + r")\b", re.I),
)

_NATIONALITIES = {
    'brazilian': ('brazil',), 'australian': ('australia',), 'aussie': ('australia', 'aussie'), 'american': ('america', 'usa', 'united states'),
    'british': ('britain', 'uk', 'england', 'united kingdom'), 'english': ('england', 'english'), 'irish': ('ireland',),
    'scottish': ('scotland',), 'welsh': ('wales',), 'canadian': ('canada',), 'mexican': ('mexico',), 'colombian': ('colombia',),
    'argentinian': ('argentina',), 'argentine': ('argentina',), 'chilean': ('chile',), 'peruvian': ('peru',), 'venezuelan': ('venezuela',),
    'cuban': ('cuba',), 'spanish': ('spain',), 'french': ('france',), 'german': ('germany',), 'italian': ('italy',),
    'portuguese': ('portugal',), 'dutch': ('netherlands', 'holland'), 'belgian': ('belgium',), 'swiss': ('switzerland',),
    'austrian': ('austria',), 'swedish': ('sweden',), 'norwegian': ('norway',), 'danish': ('denmark',), 'finnish': ('finland',),
    'polish': ('poland',), 'czech': ('czech',), 'hungarian': ('hungary',), 'romanian': ('romania',), 'bulgarian': ('bulgaria',),
    'greek': ('greece',), 'turkish': ('turkey',), 'russian': ('russia',), 'ukrainian': ('ukraine',), 'indian': ('india',),
    'pakistani': ('pakistan',), 'chinese': ('china',), 'japanese': ('japan',), 'korean': ('korea',), 'thai': ('thailand',),
    'vietnamese': ('vietnam',), 'filipina': ('philippines', 'filipin'), 'filipino': ('philippines', 'filipin'),
    'indonesian': ('indonesia',), 'malaysian': ('malaysia',), 'nigerian': ('nigeria',), 'kenyan': ('kenya',), 'ghanaian': ('ghana',),
    'egyptian': ('egypt',), 'lebanese': ('lebanon',), 'israeli': ('israel',), 'iranian': ('iran',), 'persian': ('iran', 'persia'),
    'jamaican': ('jamaica',), 'kiwi': ('new zealand', 'kiwi'),
}
_NATIONALITY_CLAIM = re.compile(r"\bi(?:'m|\s+am)\s+(?:(?:also|originally|actually|proudly|fully|part|half|quarter|a\s+bit)[\s-]+)*(?:an?\s+)?("
                                + '|'.join(sorted(_NATIONALITIES, key=len, reverse=True)) + r")\b(?!\s*(?:kiss|fries|toast|braid|manicure|press|vanilla))", re.I)

_CLOCK = r"(?:\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)|\d{1,2}:\d{2}|midnight|noon|midday)"
_TIME_CLAIM = re.compile(r"\b(?:it'?s|it\s+is)\s+(?:(?:about|almost|nearly|around|just\s+(?:past|after|gone)|already|only|exactly|literally|now)\s+)*("
                         + _CLOCK + r")(?![\w:])(?!\s*(?:somewhere|there\b|tomorrow|tonight|today|on\b|at\b|in\b|for\s+you|where\s+you|your\b|ur\b|yesterday|when\b))", re.I)
_LOCATIVE = re.compile(r"\b(?:here|for\s+me|my\s+(?:time|side)|where\s+i\s+am|on\s+my\s+end)\b", re.I)


def _owned(clause, start):
    """Words between an ownership cue ("I have", "I've got", "I grew up with", "my") and `start`, or None."""
    match = _OWN.search(clause[:start])
    if not match:
        return None
    between = match.group(1).split()
    if any(w.lower().strip(".,!?") in _OWN_BLOCK or w.lower().endswith("'s") for w in between):
        return None
    if match.group(0).lower().startswith('my') and _OTHER_PERSON.search(' '.join(between)):
        return None   # "my sister has a dog": the dog is hers
    return between


def _creator_claims(clause):
    """Yield (label, claim tokens, start, empty_ok_keys, extra) for each first-person background claim in a clause."""
    for rx in _JOB_CLAIMS:
        for match in rx.finditer(clause):
            yield 'job', _fact_tokens(_phrase(match.group(1), _JOB_CUT)), match.start(), None, None
    for match in _PET_NOUN.finditer(clause):
        between = _owned(clause, match.start())
        if between is None:
            continue
        tokens = _fact_tokens(match.group(1)) | _fact_tokens(' '.join(w for w in between if re.fullmatch(r"\d+|" + '|'.join(_NUMBER_WORDS), w, re.I)), drop_one=True)
        named = _NAMED.match(clause[match.end():])
        if named:
            tokens.add(named.group(1).lower())
        yield 'pet', tokens, match.start(), None, None
    for match in _SIBLING_NOUN.finditer(clause):
        between = _owned(clause, match.start())
        if between is None:
            continue
        tokens = _fact_tokens(match.group(1)) | _fact_tokens(' '.join(w for w in between if re.fullmatch(r"\d+|" + '|'.join(_NUMBER_WORDS), w, re.I)), drop_one=True)
        yield 'family', tokens, match.start(), None, None
    for match in _ONLY_CHILD.finditer(clause):
        yield 'family', _fact_tokens(match.group(1)), match.start(), None, None
    for rx in _EDU_CLAIMS:
        for match in rx.finditer(clause):
            yield 'education', _fact_tokens(_phrase(match.group(1), _CLAIM_CUT)), match.start(), _EDU_KEYS, None
    for match in _NATIONALITY_CLAIM.finditer(clause):
        yield 'nationality', set(), match.start(), None, _NATIONALITIES[match.group(1).lower()] + (match.group(1).lower(),)
    for match in _TIME_CLAIM.finditer(clause):
        yield 'local time', set(), match.start(), None, re.sub(r"[\s.]+", '', match.group(1).lower())


_FACT_LABEL = {'job': 'a job', 'pet': 'a pet', 'family': 'siblings or family', 'education': 'education',
               'nationality': 'a nationality', 'local time': 'the current local time'}


# Each recognised claim is grounded only in the profile fields that document that kind of fact, and only in the parts of those
# fields that describe her and are affirmative: "my sister is a nurse" is not her job, "no dogs" is not a pet. A field is
# found by its key (free-form text elsewhere is not a source); when it cannot be parsed with confidence it supports nothing.
_FIELD_KEYS = {'job': {'job', 'occupation', 'work', 'profession', 'career'},
               'pet': {'pet', 'pets', 'animal', 'animals'},
               'family': {'family', 'sibling', 'siblings', 'brother', 'brothers', 'sister', 'sisters'},
               'education': {'education', 'school', 'college', 'university', 'uni', 'degree', 'study', 'studies', 'qualification', 'qualifications'},
               'nationality': {'nationality', 'background', 'origin', 'heritage', 'ethnicity', 'birthplace', 'citizenship'}}
_OTHER_PERSON = re.compile(r"\b(?:sisters?|brothers?|siblings?|mum|mom|mother|father|dad|parents?|aunt|uncle|cousins?|friends?|boyfriend|"
                           r"girlfriend|husband|wife|partner|ex|grand\w+|nieces?|nephews?|sons?|daughters?|kids?|children|roommate|"
                           r"flatmate|neighbou?rs?|boss|colleagues?|coworkers?|twins?)\b", re.I)
_FIELD_BREAK = re.compile(r"[;.\n]+|\bbut\b|\bhowever\b|\bexcept\b|\balthough\b", re.I)
_ITEM_BREAK = re.compile(r"[,&/]|\band\b|\bplus\b", re.I)


def _affirmative_part(chunk):
    """The words of a field chunk before its first negation ("a dog named Max, no cats" -> "a dog named Max"); "none" negates all."""
    for cue in _NEGATION.finditer(chunk):
        if chunk[cue.end():cue.end() + 1] == '-':
            continue   # "no-nonsense nurse"
        return '' if cue.group(0).lower() in ('none', 'nothing', 'nobody') else chunk[:cue.start()]
    return chunk


def _documented_items(facts, label):
    """Token sets of the affirmative, creator-describing items in the fields whose key names this kind of fact."""
    items = []
    for key, value in facts:
        if not _FIELD_KEYS[label] & set(re.findall(r"[a-z]+", str(key).lower())):
            continue
        for chunk in _FIELD_BREAK.split(str(value).lower().replace('’', "'")):
            for item in _ITEM_BREAK.split(_affirmative_part(chunk)):
                if label != 'family' and _OTHER_PERSON.search(item):
                    continue   # another person's job, pet or schooling
                tokens = _fact_tokens(item)
                if tokens:
                    items.append((tokens, item))
    return items


def creator_fact_problems(texts, profile, fan_texts=()):
    """Reasons for first-person background or current-time claims her documented facts do not support."""
    facts = tuple(getattr(profile, 'facts', ()) or ())
    items = {label: _documented_items(facts, label) for label in _FIELD_KEYS}
    fan_clock = {re.sub(r"[\s.]+", '', m.group(0).lower()) for t in fan_texts for m in re.finditer(_CLOCK, t or '', re.I)}
    problems = []
    for text in texts:
        for clause, question in _clauses(text.replace('’', "'")):
            for label, tokens, start, any_keys, extra in _creator_claims(clause):
                if not _asserted(clause, question, start):
                    continue
                if label == 'local time':
                    # the fan's own time reflected back ("it's 3am, you should sleep") is his fact; "here"/"for me" makes it hers
                    if extra in fan_clock and not _LOCATIVE.search(clause):
                        continue
                    supported = False
                elif label == 'nationality':
                    own = ' '.join(item for _, item in items['nationality'])
                    supported = any(re.search(r"\b" + re.escape(term), own) for term in extra)
                elif not tokens:
                    supported = bool(any_keys) and bool(items[label])
                elif label in ('pet', 'family'):
                    supported = any(tokens <= documented for documented, _ in items[label])   # one documented item, not a pool
                else:
                    supported = tokens <= set().union(*(documented for documented, _ in items[label]))
                if not supported:
                    message = f'states {_FACT_LABEL[label]} that is not in her documented facts'
                    if message not in problems:
                        problems.append(message)
    return problems
