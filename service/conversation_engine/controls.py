"""Mechanical holds for the new offline engine.

Existing explicit platform restrictions, unresolved deliveries and durable
operator/safety holds remain in force. Legacy text detectors and sales-policy
rules do not decide what this engine may say. This module grants no send.
"""
import re
from sqlalchemy import select
from db.models import FanSafetyFlag, SkipList
from db.onlyfans_case_models import OfOperatorCase
from onlyfans_runtime.runtime_stop import HARD as SKIP_STOP


def reason(s, account_id, fan_id, state, fan, *, unresolved, commercial=False):
    if state is None or fan is None:
        return 'conversation_unavailable'
    if fan.is_muted or state.mode in {'paused', 'no_contact'}:
        return 'contact_blocked'
    if state.takeover_state == 'requested':
        return 'takeover_pending'
    if state.mode != 'automated' or state.takeover_state != 'none':
        return 'owner_changed'
    restriction = s.scalar(select(SkipList.reason).where(SkipList.account_id == account_id,
                                                          SkipList.fan_id == fan_id))
    if restriction in SKIP_STOP:   # incl. ladder_stop: every runtime_stop.STRONGER stop holds
        return 'fan_restricted'
    if unresolved:
        return 'unresolved_delivery'
    critical = s.scalar(select(FanSafetyFlag.id).where(FanSafetyFlag.account_id == account_id,
        FanSafetyFlag.fan_id == fan_id, FanSafetyFlag.severity == 'critical',
        FanSafetyFlag.resolved_at.is_(None)).limit(1))
    if critical is not None:
        return 'safety_hold'
    # Retain existing active cases as durable operator evidence. Do not create
    # new cases from legacy message detectors or infer content restrictions.
    cases = s.execute(select(OfOperatorCase.category, OfOperatorCase.scope).where(
        OfOperatorCase.account_id == account_id, OfOperatorCase.fan_id == fan_id,
        OfOperatorCase.status.in_(('open', 'acknowledged')))).all()
    if any(category == 'stop_contact' or scope == 'all' for category, scope in cases):
        return 'operator_hold'
    if commercial and any(scope == 'commercial' for _, scope in cases):
        return 'commercial_hold'
    return None


# Stop interpreter (Stage 5). A clear opt-out blocks sends through the existing no-contact path; quoted, reported,
# negated, questioning or incidental uses never do. Two tiers, both matched against a whole CLAUSE (the clause may be
# padded only by polite filler, never by other content):
#   * "directed" cessation names the fan as the object ("stop texting me", "don't text me anymore", "leave me alone",
#     "take me off the list"): it may be one clause of a longer message;
#   * "bare" words ("stop", "STOP", "unsubscribe") count only when they are the WHOLE message, because
#     "stop, you're making me blush" and "don't stop" are chat.
_STOP_PRE = r"(?:(?:hey|hi|hello|ok(?:ay)?|sorry|look|listen|seriously|honestly|just|please|pls|plz|and|so)[ ]+)*"
_STOP_POST = (r"(?:[ ]+(?:please|pls|plz|now|already|thanks|thank you|right now|for real|seriously|again|anymore|any more"
              r"|any longer|ever again|forever|ok|okay|thx))*")
_STOP_VERBS = r"(?:contact|contacting|message|messaging|text|texting|msg|msging|dm|dming|email|emailing|bother|bothering|call|calling|harass|harassing|spam|spamming|write to|writing to)"
_STOP_DIRECTED = re.compile(
    _STOP_PRE + r"(?:stop " + _STOP_VERBS + r" me"
    r"|(?:do not|don't|dont|do nt) (?:contact|message|text|msg|dm|email|bother|call|write to) me"
    r"|(?:leave|lemme|let me|just leave) (?:me )?alone"
    r"|(?:i do not|i don't|i dont) want (?:you to )?(?:contact|message|text|msg|dm|email|hear from|talk to) (?:me|you)"
    r"|(?:i do not|i don't|i dont) want (?:any more|more) (?:messages|texts|dms|emails)"
    r"|no more (?:messages|texts|dms|emails)"
    r"|(?:take|remove) me (?:off|from) (?:your|the|this) (?:list|lists|mailing list)"
    r"|unsubscribe me)" + _STOP_POST, re.I)
_STOP_BARE = re.compile(_STOP_PRE + r"(?:stop|unsubscribe|unsub|opt out|optout)" + _STOP_POST, re.I)
_STOP_QUOTES = re.compile(r"[\"\u201c\u201d]")


def _stop_clauses(value):
    out = []
    for clause in re.split(r"[.!?;,\n]+", value):
        clause = re.sub(r"[^\w']+$", '', re.sub(r"^[^\w']+", '', clause.strip()))
        clause = re.sub(r"\s+", ' ', clause)
        if clause:
            out.append(clause)
    return out


def explicit_stop(body):
    """Clear contact cessation (not every mention of the word stop); broader interpretation needs an approved model."""
    if type(body) is not str:
        return False
    # FansAPI may wrap visible text in provider HTML. Keep immutable ingress
    # unchanged and hold unsupported formatting rather than reviewing it.
    if re.search(r'<\s*/?\s*[A-Za-z]', body):
        from onlyfans_runtime.native_protocol import history_text, NativeProtocolError
        from onlyfans_runtime.store import Refused
        try:
            body = history_text(body)
        except NativeProtocolError:
            raise Refused('contact_stop_format_unclassified') from None
        if len(body) > 4000:
            raise Refused('contact_stop_format_unclassified')
    elif len(body) > 20000:
        # The whole raw text is classified, never a clipped prefix; beyond this size a person must look at it.
        from onlyfans_runtime.store import Refused
        raise Refused('contact_stop_format_unclassified')
    value = re.sub(r"\s+", " ", body.strip().lower()).replace("\u2019", "'")
    if _STOP_QUOTES.search(value):
        return False   # a quoted phrase is being discussed, not requested
    clauses = _stop_clauses(value)
    if len(clauses) == 1 and _STOP_BARE.fullmatch(clauses[0]):
        return True
    return any(_STOP_DIRECTED.fullmatch(clause) for clause in clauses)
