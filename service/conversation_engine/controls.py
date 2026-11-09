"""Mechanical holds for the new offline engine.

Existing explicit platform restrictions, unresolved deliveries and durable
operator/safety holds remain in force. Legacy text detectors and sales-policy
rules do not decide what this engine may say. This module grants no send.
"""
import re
from sqlalchemy import select
from db.models import FanSafetyFlag, SkipList
from db.onlyfans_case_models import OfOperatorCase


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
    if restriction in {'manual_restrict', 'of_restricted', 'muted_creator'}:
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


def explicit_stop(body):
    """Narrow, unambiguous opt-out; broader interpretation needs an approved model."""
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
    elif len(body) > 4000:
        return False
    value = re.sub(r"\s+", " ", body.strip().lower()).replace("’", "'")
    return re.fullmatch(r"(?:please )?(?:stop (?:contacting|messaging|texting) me|(?:do not|don't) (?:contact|message|text) me(?: again)?)[.! ]*", value) is not None
