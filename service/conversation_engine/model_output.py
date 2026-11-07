"""Judge one raw model answer: refusal, schema, then engine-level safety rules.

Layers (cheapest first): empty -> refusal/assistant language -> strict JSON schema (`model_contract.parse_proposal`)
-> proposal rules (allowed actions, offers only from this turn's approved grants, no invented prices or terms, no
purchase or "sent" claims without evidence, creator limits). The engine re-runs its own checks inside the final
transaction; this module exists so a bad answer can be regenerated (bounded) instead of failing the turn.
Regex rules are a backstop, not a classifier; real-model grounding must be measured separately.

Plan C writing rules enforced here (each one is a regression case for an observed failure):
quantities of pictures/videos come from the card's payload facts only; a question she already asked is not asked
again; an answer is never withheld until he answers or "earns" it; identity answers use only the approved wording and
make no claim about who reads or writes the chats; no discount, cheaper option or timed follow-up promise; 0-5 emojis
and no repeated opener, pet name or emoji.
"""
from dataclasses import dataclass, replace
import json
import re

from onlyfans_runtime.store import Refused
from .writer_context import classify_turn

MAX_BUBBLE_CHARS = 320  # "short to medium" (rule 5); a ceiling against essays, not a style target
MAX_BUBBLES = 4  # body plus up to three parts: the engine limit

# A model that will not write the line, or answers like an assistant instead of like her. Narrow on purpose: a
# girl's own "lol nah not doing that" must not match. (Adapted from Build B llm.py.)
REFUSAL_RX = re.compile(
    r"(language model|\bi (cannot|can'?t|won'?t|am unable to|'?m unable to) "
    r"(help with|help you with|assist|engage|continue|comply|provide|fulfill|create|write|generate|produce|roleplay|participate)\b|"
    r"\bi'?m unable to\b|\bi'?m (sorry|afraid),? (but )?i (cannot|can'?t|won'?t)\b|\bi'?m not able to (help with|assist|do that)\b|"
    r"\bi must (decline|refuse)\b|\bi apologi[sz]e,? but\b|"
    r"against (my|the|our) (guidelines|polic(y|ies)|rules)|(content|usage) polic|\bnot appropriate\b|\binappropriate\b|"
    r"this (request|content) (violates|goes against)|\bsexually explicit content\b)", re.I)
# Assistant boilerplate that is not her texting. "I am an AI assistant" is NOT matched: an accurate answer about
# the assistant's role is allowed (rule 2).
ASSISTANT_RX = re.compile(
    r"(^|\n)\s*(sure|certainly|absolutely|of course)[,!.]? (here|i)|\bhere(?:'s| is| are) (a |an |the |some |your |her )?"
    r"(reply|response|message|draft|option|version|bubbles?)|\bi'?d be happy to\b|\bi'?m here to help\b|\bhow can i (assist|help)\b|"
    r"\blet me know if (you|there)\b|\bfeel free to\b|\bi understand (that|you)\b|\*\*|^#{1,4} ", re.I | re.M)

# Platform-safety and accuracy backstops only. There is deliberately NO rule about sexual or explicit wording:
# permitted adult conversation is limited only by the creator's documented limits (profile.limit_terms).
_RULES = (
    ('promises to meet', re.compile(r"\b(meet (up|you|irl)|come over|my place|your place|see you (irl|in person)|take me out)\b", re.I)),
    ('contact details or socials', re.compile(r"[\w.+-]+@[\w-]+\.\w{2,}|(?<!\d)\+?\d[\d\s().-]{8,}\d(?!\d)|(?<![\w@])@[a-z][\w.]{2,}|"
                                              r"\b(insta(gram)?|snap(chat)?|discord|kik|wechat|signal|whatsapp|telegram)\b[: ]+@?\w", re.I)),
    ('off-platform payment or contact', re.compile(r"\b(venmo|cash ?app|paypal|zelle|my number|text me at|add me on|my snap)\b", re.I)),
    ('claims to be human or denies being automated', re.compile(
        r"\b(i'?m|im|i am)\s+(a\s+)?(really\s+)?(real|human)\b|\bnot an? (ai|bot|robot)\b|\bno agency\b|\bnot an agency\b|"
        r"\bit'?s (really |actually )?me typing\b|\breal person\b", re.I)),
    ('claims exclusivity', re.compile(r"\b(only (talking|texting|chatting) (to|with) you|you'?re the only one|just yours)\b", re.I)),
    ('relationship or future promise', re.compile(r"\b(one day we|someday we|marry (you|me)|be your girlfriend|i love you too|our future)\b", re.I)),
    ('claims content was made for him', re.compile(r"\b(made|filmed|recorded|shot|did) (this|it|that|these) (just |only |specially )?for you\b|\bjust for you\b.{0,20}\b(video|pics?|set|clip)\b", re.I)),
    ('claims to have sent or posted content', re.compile(
        r"\b(i (just )?(sent|posted|uploaded) (you )?(it|the|a|that|something)|just sent|sent you (the|a|that|it|something)|"
        r"check (your|ur) (inbox|dms|messages))\b", re.I)),
    ('left a placeholder', re.compile(r"\[[a-z_ ]{2,20}\]|\{\{|\}\}", re.I)),
    ('markdown or list formatting', re.compile(r"(^|\n)\s*(?:[-*\u2022]|\d+[.)])\s+\S|\*\*|^#{1,4} ", re.M)),
)
# --- Plan C writing rules -------------------------------------------------------------------------------------
MAX_EMOJIS = 5
_EMOJI = re.compile('[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B50\u2B55\u2764\uFE0F\U0001F000-\U0001F2FF]')
_PET_NAMES = ('babe', 'baby', 'handsome', 'daddy', 'sweetheart', 'honey', 'hun', 'love', 'sexy', 'gorgeous', 'cutie', 'stud')
# Answering is never conditional: no "answer me first", "earn it", "tell me and then".
_WITHHOLD = re.compile(
    r"\b(answer (my|the) question (first|before)|you (still )?(haven'?t|didn'?t|never) answer(ed)? (my|the)|"
    r"tell me (first|and (then|i'?ll))|(first|before (i|that)),? (you )?(tell|answer|show|prove|beg|convince)|"
    r"only if you (tell|answer|say)|not until you|until you (tell|answer|say|beg|prove)|earn (it|that|this|them|the)\b|"
    r"you (have|need|gotta|got) to (earn|convince|beg|prove|answer)|convince me|make me want to|what do i get (if|for)|"
    r"if you answer|once you answer|after you answer)", re.I)
# Claims about who reads, writes, approves or watches these chats: only the approved identity wording may say so.
_INVOLVEMENT = re.compile(
    r"\b(?:i|she|he|the creator|her team|the team|they|someone)\b.{0,24}\b(?:read|reads|reply|replies|write|writes|wrote|type|types|"
    r"typing|answer|answers|approve|approves|review|reviews|check|checks|go through)\b.{0,30}"
    r"\b(?:every|each|all|myself|herself|personally|by hand|one by one|with my own)\b|"
    r"\b(?:every|each|all)\b.{0,24}\b(?:messages?|texts?|chats?|replies)\b.{0,30}\b(?:read|reviewed|approved|written|typed|answered)\b"
    r"|\b(?:reviewed|approved|checked|written|typed|read|answered) (?:by|from) (?:her|the creator|a human|the team|a real person|me personally)\b|"
    r"\b(?:she|the creator)\b.{0,16}(?:\bis\b|'s).{0,16}\b(?:here|online|watching|involved|behind this|reading this|typing)\b|"
    r"\b(?:me|us) and (?:her|the girl|the creator|the team)\b|\b(?:it'?s|this is) (?:really |actually )?(?:me|her)\b.{0,12}\b(?:typing|writing|texting|replying)\b|"
    r"\bwe (?:both|all) (?:read|write|reply)\b", re.I)
_DISCOUNT = re.compile(
    r"\b(discount|cheaper|half[- ]price|special price|price drop|lower (the )?price|knock (some|a bit) off|\d+ ?% ?off|"
    r"for less|sale price|deal for you|bundle|payment plan|pay (me )?later)\b", re.I)
_LATER = re.compile(
    r"\b(i'?ll|i will|ill|i'?m going to|im gonna|let me) (text|message|msg|dm|write|ping|check in|get back|come back|reach out|hit you up)"
    r"( to)?( with)?( you)?( on you)? ?(later|tomorrow|tonight|again|soon|in the morning|in \d+|when|after)\b", re.I)
_NUMBER_WORDS = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
                 'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14, 'fifteen': 15, 'sixteen': 16, 'seventeen': 17,
                 'eighteen': 18, 'nineteen': 19, 'twenty': 20, 'thirty': 30, 'forty': 40, 'fifty': 50, 'dozen': 12}
_PICTURE_NOUNS = r"(?:pics?|pictures?|photos?|images?|stills?|shots?|selfies)"
_VIDEO_NOUNS = r"(?:vids?|videos?|clips?)"
_OTHER_NOUNS = r"(?:media|items?|pieces?|files?)"
_TIME_UNITS = {'minute', 'minutes', 'min', 'mins', 'second', 'seconds', 'sec', 'secs', 'hour', 'hours', 'hr', 'hrs',
               'day', 'days', 'week', 'weeks'}
_QUANTITY = re.compile(r"\b(\d{1,3}|" + '|'.join(_NUMBER_WORDS) + r")\s+(?:(\w+)\s+)??(?:(\w+)\s+)??("
                       + _PICTURE_NOUNS + '|' + _VIDEO_NOUNS + '|' + _OTHER_NOUNS + r")\b", re.I)


def _norm_words(text):
    return re.sub(r'[^a-z0-9 ]+', ' ', text.lower()).split()


def _norm_line(text):
    return ' '.join(_norm_words(text))


def quantity_claims(text):
    """[(count, kind)] for every 'N pictures / N videos / N items' statement in `text` (kind: picture|video|other)."""
    claims = []
    for match in _QUANTITY.finditer(text):
        fillers = {w.lower() for w in match.groups()[1:3] if w}
        if fillers & _TIME_UNITS:
            continue  # "a 5 minute video" is a duration, not a count
        raw = match.group(1).lower()
        count = int(raw) if raw.isdigit() else _NUMBER_WORDS[raw]
        noun = match.group(4).lower()
        kind = 'picture' if re.fullmatch(_PICTURE_NOUNS, noun) else 'video' if re.fullmatch(_VIDEO_NOUNS, noun) else 'other'
        claims.append((count, kind))
    return claims


def content_facts(snapshot, offer):
    """Quantities the payload really has, per kind. Only these numbers may be stated about content."""
    facts = {'picture': set(), 'video': set(), 'other': set(), 'history': set()}
    media = (offer or {}).get('media') or {}
    for kind, key in (('picture', 'pictures'), ('video', 'videos'), ('other', 'total')):
        if type(media.get(key)) is int:
            facts[kind].add(media[key])
    for row in snapshot.get('history', []):
        if row.get('direction') == 'out' and type(row.get('media_count')) is int and row['media_count'] > 0:
            facts['history'].add(row['media_count'])
    return facts


def _unsupported_quantities(texts, snapshot, offer):
    facts = content_facts(snapshot, offer)
    bad = []
    for text in texts:
        for count, kind in quantity_claims(text):
            if count not in facts[kind] and count not in facts['history']:
                bad.append(f'{count} {kind}')
    return bad


def _emojis(text):
    return [c for c in _EMOJI.findall(text) if c not in ('\uFE0F',)]


def _her_recent(snapshot, n=3):
    rows = [(r.get('body') or '').strip() for r in snapshot.get('history', []) if r.get('direction') == 'out']
    return [r for r in rows if r][-n:]


def _questions(text):
    return [' '.join(q.split()) for q in re.findall(r'[^.!?\n]*\?', text) if len(q.strip()) > 4]


def _same_question(a, b):
    wa, wb = set(_norm_words(a)), set(_norm_words(b))
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= 0.6


def _repetition_problems(texts, snapshot):
    problems, joined = [], ' '.join(texts)
    recent = _her_recent(snapshot)
    asked = [q for body in _her_recent(snapshot, 6) for q in _questions(body)]
    for q in _questions(joined):
        if any(_same_question(q, old) for old in asked):
            problems.append('asks a question she already asked; do not repeat it, even if he skipped it')
            break
    emojis = _emojis(joined)
    if len(emojis) > MAX_EMOJIS:
        problems.append(f'too many emojis ({len(emojis)}); use at most {MAX_EMOJIS}, often none')
    if len(recent) >= 2:
        first = (_norm_words(texts[0]) or [''])[0]
        openers = [(_norm_words(b) or [''])[0] for b in recent]
        if first and sum(1 for o in openers if o == first) >= 2:
            problems.append(f"repeats her usual opener ('{first}'); open differently")
        low = joined.lower()
        for name in _PET_NAMES:
            if re.search(r'\b' + name + r'\b', low) and sum(1 for b in recent if re.search(r'\b' + name + r'\b', b.lower())) >= 2:
                problems.append(f"repeats the pet name '{name}'; vary or drop it")
                break
        used = set(emojis)
        if used and all(used & set(_emojis(b)) for b in recent[-2:]):
            problems.append('repeats an emoji she used in each of her last two texts')
    return problems


def _norm_text(text):
    return _norm_line(clean_text(text))


def _identity_problems(texts, ctx):
    problems, joined = [], ' '.join(texts)
    wording = (ctx.identity_line or getattr(ctx.profile, 'role_statement', '') or '').strip()
    asked = classify_turn(ctx.snapshot['current_message']['body']) == 'role_question'
    if _INVOLVEMENT.search(joined) and not (wording and _norm_text(wording) in _norm_text(joined)):
        problems.append('makes a claim about who reads, writes or approves the chats that is not in the approved identity wording')
    if asked:
        if not wording:
            problems.append('identity question without approved identity wording; a human must answer')
        elif _norm_text(wording) not in _norm_text(joined):
            problems.append('answer his identity question with exactly the approved identity wording')
    return problems


def _writing_problems(proposal, ctx, texts):
    from .writer_context import offer_for
    problems, joined = [], ' '.join(texts)
    snapshot = ctx.snapshot
    if _WITHHOLD.search(joined):
        problems.append('makes an answer or item conditional on him answering, tell her something or earning it; answer first')
    if _DISCOUNT.search(joined):
        problems.append('offers a discount, cheaper option or bundle; the price is fixed and there is one product')
    if _LATER.search(joined):
        problems.append('promises to message him later; there is no timed follow-up')
    offer = offer_for(snapshot, ctx.profile, snapshot['current_message']['body']) if hasattr(ctx.profile, 'may_offer') else None
    bad = _unsupported_quantities(texts, snapshot, offer)
    if bad:
        problems.append('states a content quantity that is not in the card facts (' + ', '.join(bad[:3]) + '); use only CONTENTS numbers or none')
    problems.extend(_identity_problems(texts, ctx))
    problems.extend(_repetition_problems(texts, snapshot))
    if snapshot.get('journey'):
        from .journey import journey_problems
        problems.extend(journey_problems(snapshot['journey'], proposal))
    return problems


_AGE = re.compile(r"\b(i'?m|im|i am) \d{2}\b(?! ?(min|mins|minutes|sec|secs|hrs?|hours?|%|more|percent))|\b\d{2} ?(yo|y/o|years? old)\b", re.I)
_LOCATION = re.compile(r"\b(i live in|i'?m from|im from|my (real name|address) is)\b", re.I)


def extract_json_text(raw):
    """Strip exactly one surrounding code fence; everything else must already be strict JSON."""
    text = raw.strip()
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.S | re.I)
    return match.group(1) if match else text


def clean_text(value):
    """Mechanical clean of the model's wording: no em/en dashes or semicolons, tidy spaces."""
    value = value.replace('—', ', ').replace('–', ', ').replace(';', ',')
    value = re.sub(r'\s+,', ',', value)
    value = re.sub(r'[ \t]{2,}', ' ', value).strip().strip('"').strip()
    return value


@dataclass(frozen=True)
class JudgeContext:
    snapshot: dict
    profile: object  # CreatorProfile (writer_prompt)
    allowed_grant_ids: frozenset = frozenset()
    allowed_scope_ids: frozenset = frozenset()
    identity_line: str = ''   # APPROVED identity wording ('' = none)


@dataclass(frozen=True)
class Verdict:
    outcome: str  # ok | empty | refusal | malformed | invalid
    proposal: object = None
    reasons: tuple = ()


def _texts(proposal):
    return [proposal.body, *proposal.parts]


def _effective_sales_mode(snapshot, proposal):
    mode = snapshot['sales_mode']
    if proposal.sales_signal == 'decline':
        return 'paused'
    if proposal.sales_signal == 'reopen' and mode != 'open':
        return 'reopened_scoped'
    return mode


def proposal_problems(proposal, ctx):
    """Return a list of human-readable problems (empty = acceptable). Also used as regeneration notes."""
    problems = []
    texts = _texts(proposal)
    profile = ctx.profile
    snapshot = ctx.snapshot
    if len(texts) > MAX_BUBBLES:
        problems.append(f'too many messages ({len(texts)}); at most {MAX_BUBBLES}, one is better')
    if any(len(text) > MAX_BUBBLE_CHARS for text in texts):
        problems.append('a message is too long; write short texts, not paragraphs')
    if proposal.action == 'offer':
        if proposal.parts:
            problems.append('an offer turn is one message; put everything in body')
        if proposal.offer_grant_id not in ctx.allowed_grant_ids:
            problems.append('offer_grant_id is not the approved offer for this turn')
    elif proposal.offer_grant_id is not None:
        problems.append('offer_grant_id is only for action offer')
    if proposal.sales_signal == 'reopen' and not proposal.item_scope:
        problems.append('reopen needs item_scope naming the item he explicitly asked for')
    if proposal.sales_signal != 'reopen' and proposal.item_scope:
        problems.append('item_scope is only for sales_signal reopen')
    if proposal.sales_signal == 'reopen' and not set(proposal.item_scope) <= ctx.allowed_scope_ids:
        problems.append('item_scope names an item that is not in the approved catalogue match')
    from .claims import check_plain_text, grounding_from_snapshot
    try:
        wording = (ctx.identity_line or getattr(ctx.profile, 'role_statement', '') or '').strip()
        # The approved identity wording is the one sanctioned statement about who writes; it is not re-judged as a claim.
        checked = [t.replace(wording, '') for t in texts] if wording else texts
        check_plain_text(checked, proposal, _effective_sales_mode(snapshot, proposal),
                         {offer['priceCents'] for offer in snapshot['offer_reference']['offers']},
                         grounding=grounding_from_snapshot(snapshot, proposal))
    except Refused as exc:
        problems.append(str(exc))
    joined = ' '.join(texts)
    for name, rx in _RULES:
        match = rx.search(joined)
        if match:
            problems.append(f"{name} ('{match.group(0).strip()[:40]}')")
    age = profile.fact('age') if hasattr(profile, 'fact') else None
    found_age = _AGE.search(joined)
    if found_age and (age is None or age not in found_age.group(0)):
        problems.append('states an age that is not in her documented facts')
    if _LOCATION.search(joined) and not (profile.fact('home_city') if hasattr(profile, 'fact') else None):
        problems.append('states where she lives or is from; no such fact is documented')
    problems.extend(_writing_problems(proposal, ctx, texts))
    low = joined.lower()
    for term in getattr(profile, 'private_terms', ()) or ():
        if term and re.search(r'\b' + re.escape(term.lower()) + r'\b', low):
            problems.append(f"reveals private information ('{term}')")
    for phrase in getattr(profile, 'avoid_phrases', ()) or ():
        if phrase and phrase.lower() in low:
            problems.append(f"uses a banned line ('{phrase}')")
    for term in getattr(profile, 'limit_terms', ()) or ():
        if term and re.search(r'\b' + re.escape(term.lower()) + r'\b', low):
            problems.append(f"crosses one of her documented limits ('{term}')")
    return problems


def judge(raw, ctx):
    """Turn raw model text into a Verdict. Never raises for bad model output."""
    from .model_contract import parse_proposal
    if type(raw) is not str or not raw.strip():
        return Verdict('empty', reasons=('empty answer',))
    text = extract_json_text(raw)
    try:
        proposal = parse_proposal(text)
    except Refused:
        if REFUSAL_RX.search(raw) or ASSISTANT_RX.search(raw):
            return Verdict('refusal', reasons=('answered like an assistant or refused instead of writing her reply',))
        return Verdict('malformed', reasons=('answer was not the required strict JSON object',))
    proposal = replace(proposal, body=clean_text(proposal.body), parts=tuple(clean_text(p) for p in proposal.parts))
    if not proposal.body or any(not part for part in proposal.parts):
        return Verdict('empty', reasons=('empty message text',))
    joined = ' '.join(_texts(proposal))
    if REFUSAL_RX.search(joined) or ASSISTANT_RX.search(joined):
        return Verdict('refusal', reasons=('message text reads as a refusal or an assistant answer',))
    problems = proposal_problems(proposal, ctx)
    if problems:
        return Verdict('invalid', proposal=proposal, reasons=tuple(problems))
    return Verdict('ok', proposal=proposal)
