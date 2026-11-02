"""Per-account recent-history policy; no deletion, sending or rate/budget changes.

The environment allowlist is a deployment gate; the durable row freezes the
cutoff. Disabling either gate restores the lifetime scheduler without erasing
any cursor, receipt or canonical data.
"""
from datetime import timedelta
import json
import os
import re

from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Column, DateTime,
                        ForeignKey, Integer, String, and_, exists, or_, select, text)
from db.models import Base, Message
from .store import Refused, utcnow

INFO = {"onlyfans_since": "0110_recent_history"}


class OfHistoryPolicy(Base):
    __tablename__ = 'of_history_policy'
    __table_args__ = (
        CheckConstraint('cutoff_at <= started_at', name='window'),
        CheckConstraint('writing_sample_limit >= 0 AND writing_sample_limit <= 100', name='style_sample'),
        CheckConstraint('NOT timestamp_order_verified OR (ordering_evidence IS NOT NULL AND length(trim(ordering_evidence)) > 0)', name='ordering_evidence'),
        {"info": INFO})
    account_id = Column(String, ForeignKey('of_runtime.account_id', ondelete='RESTRICT'), primary_key=True)
    binding_id = Column(String, ForeignKey('of_creator_bindings.id', ondelete='RESTRICT'), nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    started_at = Column(DateTime, nullable=False)
    cutoff_at = Column(DateTime, nullable=False)
    timestamp_order_verified = Column(Boolean, nullable=False, default=False)
    ordering_evidence = Column(String(512))
    writing_sample_limit = Column(Integer, nullable=False, default=0)
    last_older_at = Column(DateTime)
    updated_at = Column(DateTime, nullable=False)


class OfOlderHistoryJob(Base):
    __tablename__ = 'of_older_history_job'
    __table_args__ = ({"info": INFO},)
    account_id = Column(String, ForeignKey('of_runtime.account_id', ondelete='RESTRICT'), primary_key=True)
    fan_id = Column(BigInteger, primary_key=True)
    binding_id = Column(String, ForeignKey('of_creator_bindings.id', ondelete='RESTRICT'), nullable=False)
    requested_at = Column(DateTime, nullable=False)
    priority = Column(Integer, nullable=False, default=2)
    reason = Column(String(32), nullable=False)


def configured(account):
    value = json.loads(os.environ.get('ONLYFANS_RECENT_HISTORY_ACCOUNTS', '[]'))
    if type(value) is not list or any(type(x) is not str for x in value):
        raise Refused('invalid recent-history account allowlist')
    return account in value


def automatic_new_accounts():
    value = os.environ.get('ONLYFANS_RECENT_HISTORY_NEW_ACCOUNTS', '0')
    if value not in {'0', '1'}:
        raise Refused('invalid recent-history new-account switch')
    return value == '1'


def policy(s, account, binding=None):
    if not configured(account) and not automatic_new_accounts():
        return None
    row = s.get(OfHistoryPolicy, account)
    if row is None or not row.enabled:
        return None
    if binding is not None and row.binding_id != binding:
        raise Refused('recent-history binding changed')
    return row


def ensure(s, runtime, *, new_account=False):
    explicit = configured(runtime.account_id)
    automatic = automatic_new_accounts()
    if not explicit and not automatic:
        return None
    row = s.get(OfHistoryPolicy, runtime.account_id)
    # The global switch admits new connections only. It must not enroll an
    # existing account when the importer first sees it after a rollout.
    if row is None and not explicit and not (automatic and new_account):
        return None
    if row is None:
        now = runtime.activated_at if new_account else utcnow()
        row = OfHistoryPolicy(account_id=runtime.account_id, binding_id=runtime.binding_id,
            enabled=True, started_at=now, cutoff_at=now-timedelta(days=30), updated_at=now)
        s.add(row)
        s.flush()
    if row.binding_id != runtime.binding_id:
        raise Refused('recent-history binding changed')
    return row if row.enabled else None


def activate_older(s, runtime, event):
    """Called only for committed live inbound messages; one durable pair key.

    Returning-fan history is read-only even when automation/takeover is paused.
    Priority hints affect retrieval scheduling only, never payment entitlement.
    """
    p = policy(s, runtime.account_id, runtime.binding_id)
    if p is None:
        return
    from .launch_history_memory.recovery import HistoryRecovery, OfHistoryCursor
    cursor = HistoryRecovery._ensure_fan(s, runtime, event.fan_id)
    if cursor.status == 'complete':
        return
    reason = ('payment_context' if re.search(r'\b(paid|bought|purchase|unlock|refund)\b', event.body, re.I)
              else 'prior_reference' if re.search(r'\b(remember|last time|before|promis\w*|owe\w*)\b', event.body, re.I)
              else 'returning_fan')
    priority = 0 if reason == 'payment_context' else 1 if reason == 'prior_reference' else 2
    row = s.get(OfOlderHistoryJob, (runtime.account_id, event.fan_id))
    if row is None:
        s.add(OfOlderHistoryJob(account_id=runtime.account_id, binding_id=runtime.binding_id,
            fan_id=event.fan_id, requested_at=utcnow(), priority=priority, reason=reason))
    elif row.binding_id != runtime.binding_id:
        raise Refused('older-history binding changed')
    elif priority < row.priority:
        row.priority, row.reason = priority, reason


def allowed_messages(s, account, binding, model=Message):
    p = policy(s, account, binding)
    if p is None:
        return True
    activated = exists(select(OfOlderHistoryJob.fan_id).where(
        OfOlderHistoryJob.account_id == model.account_id, OfOlderHistoryJob.fan_id == model.fan_id,
        OfOlderHistoryJob.binding_id == binding))
    return or_(model.created_at >= p.cutoff_at, activated)


def source_filter(s, account, binding, fan, *, alias='m'):
    """Scope raw queue SQL using a fixed, validated alias and bound cutoff."""
    if alias not in {'m'}:
        raise ValueError('invalid source alias')
    p = policy(s, account, binding)
    if p is None:
        return '1=1', {}
    if not fan:
        # Creator knowledge is approved by the existing personality workflow.
        # Archive claims never supply creator facts. Samples are style-only.
        return '1=0', {}
    return f'''({alias}.created_at >= :recent_cutoff OR EXISTS (
        SELECT 1 FROM of_older_history_job h WHERE h.account_id={alias}.account_id
        AND h.fan_id={alias}.fan_id AND h.binding_id=:binding_id))''', {'recent_cutoff': p.cutoff_at}


def approved_onboarding(s, account, binding):
    from .launch_personality.models import PersonalityHead, PersonalityRevision
    head = s.get(PersonalityHead, account)
    row = s.get(PersonalityRevision, (account, head.revision)) if head and head.binding_id == binding else None
    if row is None or row.state != 'approved':
        return None
    # A later draft cannot silently become runtime authority.
    return json.loads(row.document_json).get('approvedOnboarding')


def memory_notice(s, account, binding, fan):
    if policy(s, account, binding) is None:
        return None
    from .launch_history_memory.recovery import OfHistoryCursor
    cursor = s.get(OfHistoryCursor, (account, fan))
    complete = bool(cursor and cursor.binding_id == binding and cursor.status == 'complete'
                    and cursor.start_before_id is None)
    return {'lifetimeHistoryComplete': complete,
        'instruction': 'Use only available evidence. Do not claim to remember unavailable history. '
        'Missing payment, access or fulfilment evidence requires the existing verification or human-review path. '
        'Chat text is never payment proof. Older history may arrive after this reply.'}
