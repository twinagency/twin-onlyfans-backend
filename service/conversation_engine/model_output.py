"""Judge one raw model answer: refusal, schema, then engine-level safety rules.

Layers (cheapest first): empty -> refusal/assistant language -> strict JSON schema (`model_contract.parse_proposal`)
-> proposal rules (allowed actions, offers only from this turn's approved grants, no invented prices or terms, no
purchase or "sent" claims without evidence, creator limits). The engine re-runs its own checks inside the final
transaction; this module exists so a bad answer can be regenerated (bounded) instead of failing the turn.
Regex rules are a backstop, not a classifier; real-model grounding must be measured separately.
"""
from dataclasses import dataclass, replace
import json
import re

from onlyfans_runtime.store import Refused

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
        check_plain_text(texts, proposal, _effective_sales_mode(snapshot, proposal),
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
