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


def grounding_from_snapshot(snapshot, proposal=None):
    """Build Grounding from a writer snapshot. `offer_grants`/`verified_purchases` exist only for a real model."""
    quantities = set()
    grants = snapshot.get('offer_grants') or []
    wanted = getattr(proposal, 'offer_grant_id', None)
    if proposal is not None and getattr(proposal, 'action', 'reply') == 'offer':
        grants = [g for g in grants if g.get('grantId') == wanted]
    for grant in grants:
        if type(grant.get('mediaCount')) is int and grant['mediaCount'] > 0:
            quantities.add(grant['mediaCount'])
        media = grant.get('media')
        if isinstance(media, dict):   # payload facts: total plus per-kind counts (the writer judge checks which noun)
            for key in ('total', 'pictures', 'videos'):
                if type(media.get(key)) is int and media[key] > 0:
                    quantities.add(media[key])
    for row in snapshot.get('history') or []:
        if row.get('direction') == 'out' and type(row.get('media_count')) is int and row['media_count'] > 0:
            quantities.add(row['media_count'])
    for item in (snapshot.get('catalogue_search') or {}).get('items') or []:
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
    prices = frozenset(g['priceCents'] for g in [*(snapshot.get('offer_grants') or []), *(snapshot.get('price_information') or [])]
                       if isinstance(g, dict) and type(g.get('priceCents')) is int
                       and g['priceCents'] > 0 and g.get('currency') == 'USD')
    access_labels = tuple(card['label'] for card in (snapshot.get('offer_reference') or {}).get('cards') or []
                          if isinstance(card, dict) and isinstance(card.get('label'), str)
                          and card.get('sendState') == 'accepted' and card.get('accessState') == 'confirmed'
                          and card.get('financialReview') is False)
    return Grounding(frozenset(quantities), labels, media_reference, prices, access_labels)


_NUMBER_WORDS = {'zero': 0, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8,
                 'nine': 9, 'ten': 10, 'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14, 'fifteen': 15,
                 'sixteen': 16, 'seventeen': 17, 'eighteen': 18, 'nineteen': 19, 'twenty': 20, 'thirty': 30,
                 'forty': 40, 'fifty': 50, 'sixty': 60, 'seventy': 70, 'eighty': 80, 'ninety': 90,
                 'hundred': 100}
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
    re.compile(r"\byou\s+(?:now\s+|already\s+)?own\b", re.I),
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
_QUANTITY = re.compile(r"(?<![\d.,$\u00a3\u20ac])\b(" + _NUMBER_RX + r")(?:\s+(?:more|extra|new|other|different))?\s+(?:" + _FILLER + r"[a-z]+\s+){0,2}?" + _CONTENT_NOUN + r"\b", re.I)


def _quantity_claims(text):
    for clause, _question in _clauses(text):
        for match in _QUANTITY.finditer(clause):
            raw = match.group(1).lower()
            number = int(raw) if raw.isdigit() else _NUMBER_WORDS[raw]
            # "one" is the ordinary pronoun/determiner far more often than a count; only digits or a noun right after it count
            if raw == 'one' and not re.match(r"one\s+(?:" + _FILLER + r"\w+\s+)?" + _CONTENT_NOUN + r"\b", match.group(0), re.I):
                continue
            if re.search(r"\b(?:minutes?|mins?|seconds?|secs?|hours?|hrs?|days?|weeks?|months?|years?)\b", match.group(0), re.I):
                continue   # a duration ("a 10 minute video") is not a count of items
            if _negated(clause, match.start()):
                continue
            yield number


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
            hit = bool(verb and subject and scope and not _negated(clause, verb.start()) and subject.start() < scope.end())
        if hit:
            yield clause


def _check_quantities(texts, grounding):
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
    _check_quantities(texts, grounding)
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
        if next(_custom_promises(text), None) is not None:
            raise Refused('promises a custom that is not supported')
        if next(_involvement_claims(text), None) is not None:
            raise Refused('unsupported claim about who writes or reviews messages')
