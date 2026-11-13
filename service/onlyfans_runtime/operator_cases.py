"""Durable operator cases and the conversation-safety writer (R1).

Extends, does not replace: the conversation CONTROL stays ``Store.control`` (paused / no_contact, audited, schedule
and engagement invalidation); the human-attention flag stays ``requires_attention``; ownership stays the takeover
audit (``handover.takeover_owner``). This module adds the missing record: reason, evidence, scope, owner, due time,
acknowledgement, status, resolution and escalation, in ``of_operator_case``, plus

* ``policy_facts`` / ``policy_facts_sql``: the extra rows the shared policy reads (skip_list, safety flag, open cases);
* ``scan``: run the shared detectors over the fan's recent inbound messages and record (shadow) or enforce (live);
* ``acknowledge`` / ``resolve`` / ``list_cases`` / ``sweep_overdue``: the minimal operator surface.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from db.models import AccountAiConfig, Fan, FanSafetyFlag, Message, OfConversationState, OfMessageDelivery, SkipList
from db.onlyfans_case_models import OfOperatorCase
from .conversation_policy import CATEGORY_SCOPE, RESUME_BLOCKING
from db.onlyfans_delivery_models import OfTextIntent
from .store import Refused, utcnow

OPEN = ("open", "acknowledged")
SCAN_WINDOW_HOURS = 24
SCAN_LIMIT = 10
EXCERPT = 200
# Due time from creation. Missing staffing blocks live activation of a handover-dependent account; it does not
# change these clocks. An overdue open case escalates one level per interval (sweep_overdue).
SLA_SECONDS = {"minor": 900, "distress": 900, "consent": 1800, "identity": 1800, "stop_contact": 3600,
               "complaint": 4 * 3600, "hardship": 24 * 3600, "external_sender": 3600, "other": 24 * 3600}
ACTOR = "safety-guard"
EXTERNAL_GRACE_SECONDS = 60       # our own send's projection may lag the provider echo; younger rows are not judged
EXTERNAL_WINDOW_HOURS = 6


def _due(category, now):
    return now + timedelta(seconds=SLA_SECONDS.get(category, SLA_SECONDS["other"]))


# ── policy facts ──────────────────────────────────────────────────────────────────────────────────────────────────
def policy_facts(s, account_id, fan_id):
    """(skip_reason, critical_flag, cases) for the shared policy, from a SQLAlchemy session."""
    skip = s.scalar(select(SkipList.reason).where(SkipList.account_id == account_id, SkipList.fan_id == fan_id))
    critical = s.scalar(select(FanSafetyFlag.id).where(
        FanSafetyFlag.account_id == account_id, FanSafetyFlag.fan_id == fan_id,
        FanSafetyFlag.severity == "critical", FanSafetyFlag.resolved_at.is_(None)).limit(1)) is not None
    cases = frozenset((c, sc) for c, sc in s.execute(select(OfOperatorCase.category, OfOperatorCase.scope).where(
        OfOperatorCase.account_id == account_id, OfOperatorCase.fan_id == fan_id,
        OfOperatorCase.status.in_(OPEN))).all())
    return skip, critical, cases


def policy_facts_sql(con, account_id, fan_id, has_table):
    """The same three facts from a qmark connection (the FASTT runner's database handle)."""
    skip, critical, cases = None, False, set()
    if has_table(con, "skip_list"):
        row = con.execute("SELECT reason FROM skip_list WHERE account_id=? AND fan_id=?", (account_id, fan_id)).fetchone()
        skip = row[0] if row else None
    if has_table(con, "fan_safety_flags"):
        critical = con.execute("SELECT 1 FROM fan_safety_flags WHERE account_id=? AND fan_id=? AND severity='critical' "
                               "AND resolved_at IS NULL LIMIT 1", (account_id, fan_id)).fetchone() is not None
    if has_table(con, "of_operator_case"):
        cases = {(r[0], r[1]) for r in con.execute(
            "SELECT category,scope FROM of_operator_case WHERE account_id=? AND fan_id=? AND status IN ('open','acknowledged')",
            (account_id, fan_id)).fetchall()}
    return skip, critical, frozenset(cases)


def blocking_open_case(s, account_id, fan_id):
    """The category of an open case that forbids resuming automation, or None."""
    return s.scalar(select(OfOperatorCase.category).where(
        OfOperatorCase.account_id == account_id, OfOperatorCase.fan_id == fan_id,
        OfOperatorCase.status.in_(OPEN), OfOperatorCase.category.in_(tuple(RESUME_BLOCKING))).limit(1))


# ── writing ───────────────────────────────────────────────────────────────────────────────────────────────────────
def open_case(s, account_id, fan_id, category, *, reason, evidence, source_message_id=None, origin="detector",
              scope=None, status="open", now=None, owner=None, due_at=None):
    """Create (or return) the one active case for this fan and category. Must run inside the caller's transaction."""
    now = now or utcnow()
    scope = scope or CATEGORY_SCOPE.get(category, "all")
    existing = s.scalar(select(OfOperatorCase).where(
        OfOperatorCase.account_id == account_id, OfOperatorCase.fan_id == fan_id,
        OfOperatorCase.category == category, OfOperatorCase.status.in_(OPEN)).limit(1))
    if existing is not None and status in OPEN:
        return existing, False
    if source_message_id is not None:
        same = s.scalar(select(OfOperatorCase).where(
            OfOperatorCase.account_id == account_id, OfOperatorCase.fan_id == fan_id,
            OfOperatorCase.category == category, OfOperatorCase.source_message_id == source_message_id).limit(1))
        if same is not None:
            if same.status == "shadow" and status in OPEN:      # shadow became live: enforce this verdict
                same.status, same.due_at, same.updated_at = status, due_at or _due(category, now), now
                same.owner = same.owner or owner or _takeover_owner(s, account_id, fan_id)
                return same, True
            return same, False
    row = OfOperatorCase(account_id=account_id, case_id=str(uuid.uuid4()), fan_id=fan_id, category=category,
        scope=scope, status=status, reason=reason[:64], evidence_json=json.dumps(evidence, sort_keys=True)[:4000],
        source_message_id=source_message_id, origin=origin,
        owner=owner or (_takeover_owner(s, account_id, fan_id) if status in OPEN else None),
        due_at=(due_at or _due(category, now)) if status in OPEN else None,
        escalation_level=0, created_at=now, updated_at=now)
    s.add(row)
    s.flush()
    if status in OPEN:
        state = s.get(OfConversationState, (account_id, fan_id))
        if state is not None:
            state.requires_attention = True
            state.updated_at = now
    return row, status in OPEN


def _takeover_owner(s, account_id, fan_id):
    from .handover import takeover_owner
    state = s.get(OfConversationState, (account_id, fan_id))
    try:
        return takeover_owner(s, account_id, fan_id, state)
    except Exception:
        return None


def _control_key(category, account_id, fan_id, case_id):
    return hashlib.sha256(f"safety:{category}:{account_id}:{fan_id}:{case_id}".encode()).hexdigest()[:64]


def apply_control(store, tenant_id, account_id, fan_id, category, case_id):
    """The control side of an enforced hold, through the existing audited ``Store.control``.

    stop_contact -> no_contact; distress / consent / identity -> pause (only while automated; a human who already
    owns the chat keeps it). The case is already committed, so a refused or lost control never loosens the hold.
    """
    for _ in range(2):
        with Session(store.engine) as s:
            state = s.get(OfConversationState, (account_id, fan_id))
            if state is None:
                return None
            mode, revision = state.mode, state.revision
        if category == "stop_contact":
            if mode == "no_contact":
                return None
            target, action = "no_contact", "mark_no_contact"
        elif mode == "automated":
            target, action = "paused", "pause_automation"
        else:
            return None
        try:
            return store.control(tenant_id, account_id, fan_id, mode=target, expected_revision=revision,
                                 command_key=_control_key(category, account_id, fan_id, case_id), actor=ACTOR,
                                 reason=f"Conversation safety hold: {category}", action=action)
        except Refused as exc:
            if "revision" in str(exc):
                continue            # the conversation moved; look again once
            return None
    return None


def hold(store, tenant_id, account_id, fan_id, category, *, reason, evidence, source_message_id=None, origin="detector"):
    """Open the case and apply the control. Used by the identity guard, the detectors and the external-sender record."""
    with Session(store.engine) as s, s.begin():
        case, _ = open_case(s, account_id, fan_id, category, reason=reason, evidence=evidence,
                            source_message_id=source_message_id, origin=origin)
        case_id = case.case_id
    if CATEGORY_SCOPE.get(category, "all") == "all":
        apply_control(store, tenant_id, account_id, fan_id, category, case_id)
    return case_id


# ── detection ─────────────────────────────────────────────────────────────────────────────────────────────────────
def _mode(s, account_id):
    from .configuration import object_value
    from .safety_mode import mode
    raw = s.scalar(select(AccountAiConfig.ai_chatter_config_json).where(AccountAiConfig.account_id == account_id))
    try:
        return mode(object_value(raw))
    except (ValueError, TypeError, RecursionError):
        return "shadow"


def scan(store, tenant_id, account_id, fan_id, *, now=None):
    """Run the shared detectors over the fan's recent inbound messages. Returns the categories newly enforced.

    Called by every non-manual sender BEFORE it opens its own transaction (prepare / admit / consume_dispatch and the
    FASTT turn), so a stop request that arrives after planning is seen again immediately before the wire. ``shadow``
    only records; ``live`` writes cases and controls. Identity is enforced in every mode.
    """
    from native_pilot.safety_detect import assess, IDENTITY, HOLD_CATEGORIES
    now = now or utcnow()
    with Session(store.engine) as s:
        mode = _mode(s, account_id)
        rows = s.execute(select(Message.message_id, Message.body).where(
            Message.account_id == account_id, Message.fan_id == fan_id, Message.direction == "in",
            Message.is_unsent.is_(False), Message.created_at > now - timedelta(hours=SCAN_WINDOW_HOURS))
            .order_by(Message.created_at.desc(), Message.message_id.desc()).limit(SCAN_LIMIT)).all()
    enforced = _observe_external(store, account_id, fan_id, mode, now)
    if not rows:
        return _apply_enforced(store, tenant_id, account_id, fan_id, enforced)
    rows = list(reversed(rows))
    verdicts = assess([r[1] or "" for r in rows])
    if not verdicts:
        return _apply_enforced(store, tenant_id, account_id, fan_id, enforced)
    with Session(store.engine) as s, s.begin():
        if s.get(Fan, (account_id, fan_id)) is None:
            return []
        for v in verdicts:
            message_id, body = rows[v.message_index]
            live = mode == "live" or v.category == IDENTITY
            if mode == "off" and v.category != IDENTITY:
                continue
            status = "open" if (v.holds and live) else "shadow"
            evidence = {"rule": v.code, "ambiguous": v.ambiguous, "messageIds": [message_id],
                        "excerpt": (body or "")[:EXCERPT], "mode": mode}
            row, changed = open_case(s, account_id, fan_id, v.category, reason=v.code, evidence=evidence,
                                     source_message_id=message_id, origin="detector", scope=v.scope, status=status, now=now)
            if status == "open" and changed:
                enforced.append((v.category, row.case_id))
    return _apply_enforced(store, tenant_id, account_id, fan_id, enforced)


def _apply_enforced(store, tenant_id, account_id, fan_id, enforced):
    for category, case_id in enforced:
        if CATEGORY_SCOPE.get(category, "all") == "all":
            apply_control(store, tenant_id, account_id, fan_id, category, case_id)
    return [c for c, _ in enforced]


def _observe_external(store, account_id, fan_id, mode, now):
    """Record outbound messages that none of OUR senders wrote (Infloww, the OnlyFans app, another tool).

    No external writer can share an enforceable authority with us, so the strongest honest thing is to OBSERVE it into
    the same durable conversation state both planes read: ``live`` opens an ``external_sender`` case and pauses the
    chat (the existing control cancels queued engagement work and invalidates plans and queued intents by revision);
    ``shadow`` only records the observation. See docs/onlyfans/final-repair/R1-EXTERNAL-SENDERS.md.
    """
    if mode == "off":
        return []
    ours = select(OfMessageDelivery.message_id).where(OfMessageDelivery.account_id == Message.account_id,
        OfMessageDelivery.fan_id == Message.fan_id, OfMessageDelivery.message_id == Message.message_id)
    sent = select(OfTextIntent.intent_id).where(OfTextIntent.account_id == Message.account_id,
        OfTextIntent.provider_message_id == Message.message_id)
    with Session(store.engine) as s, s.begin():
        rows = s.execute(select(Message.message_id, Message.body).where(
            Message.account_id == account_id, Message.fan_id == fan_id, Message.direction == "out",
            Message.automation_kind.is_(None), Message.sent_by_employee_id.is_(None), Message.mass_run_id.is_(None),
            Message.is_unsent.is_(False), Message.created_at > now - timedelta(hours=EXTERNAL_WINDOW_HOURS),
            Message.created_at < now - timedelta(seconds=EXTERNAL_GRACE_SECONDS),
            ~ours.exists(), ~sent.exists()).order_by(Message.created_at.desc()).limit(3)).all()
        enforced = []
        for message_id, body in rows:
            status = "open" if mode == "live" else "shadow"
            evidence = {"rule": "external-outbound-observed", "messageIds": [message_id], "mode": mode,
                        "length": len(body or "")}
            row, changed = open_case(s, account_id, fan_id, "external_sender", reason="external-outbound-observed",
                evidence=evidence, source_message_id=message_id, origin="external", scope="all", status=status, now=now)
            if status == "open" and changed:
                enforced.append(("external_sender", row.case_id))
    return enforced


# ── operator surface ──────────────────────────────────────────────────────────────────────────────────────────────
def _view(row, now):
    overdue = row.status in OPEN and row.due_at is not None and row.due_at < now
    return {"caseId": row.case_id, "accountId": row.account_id, "fanId": row.fan_id, "category": row.category,
            "scope": row.scope, "status": row.status, "reason": row.reason,
            "evidence": json.loads(row.evidence_json or "{}"), "owner": row.owner,
            "dueAt": row.due_at.isoformat() if row.due_at else None, "overdue": overdue,
            "acknowledgedAt": row.acknowledged_at.isoformat() if row.acknowledged_at else None,
            "acknowledgedBy": row.acknowledged_by, "resolution": row.resolution,
            "resolvedAt": row.resolved_at.isoformat() if row.resolved_at else None, "resolvedBy": row.resolved_by,
            "escalationLevel": row.escalation_level,
            "createdAt": row.created_at.isoformat()}


def list_cases(s, account_id, *, fan_id=None, include_resolved=False, limit=100, now=None):
    now = now or utcnow()
    q = select(OfOperatorCase).where(OfOperatorCase.account_id == account_id)
    if fan_id is not None:
        q = q.where(OfOperatorCase.fan_id == fan_id)
    q = q.where(OfOperatorCase.status != "shadow")
    if not include_resolved:
        q = q.where(OfOperatorCase.status.in_(OPEN))
    rows = s.scalars(q.order_by(OfOperatorCase.due_at.asc().nulls_last(), OfOperatorCase.created_at.desc()).limit(min(limit, 500))).all()
    return [_view(r, now) for r in rows]


def acknowledge(store, tenant_id, account_id, case_id, *, actor, owner=None):
    """Take ownership: status acknowledged, the actor (or an explicit owner) becomes the owner. Idempotent."""
    if not isinstance(actor, str) or not 1 <= len(actor) <= 128:
        raise Refused("invalid case actor")
    with Session(store.engine) as s, s.begin():
        store._runtime(s, tenant_id, account_id)
        row = s.get(OfOperatorCase, (account_id, case_id))
        if row is None or row.status == "shadow":
            raise Refused("case unavailable")
        if row.status == "resolved":
            raise Refused("case already resolved")
        now = utcnow()
        if row.status == "open":
            row.status, row.acknowledged_at, row.acknowledged_by = "acknowledged", now, actor
        row.owner = owner or row.owner or actor
        row.updated_at = now
        return _view(row, now)


def resolve(store, tenant_id, account_id, case_id, *, actor, resolution):
    """Record the outcome. Resolving lifts only this case; resuming automation is still the audited control."""
    if not isinstance(actor, str) or not 1 <= len(actor) <= 128 or not isinstance(resolution, str) or not 1 <= len(resolution.strip()) <= 2000:
        raise Refused("invalid case resolution")
    with Session(store.engine) as s, s.begin():
        store._runtime(s, tenant_id, account_id)
        row = s.get(OfOperatorCase, (account_id, case_id))
        if row is None or row.status == "shadow":
            raise Refused("case unavailable")
        now = utcnow()
        if row.status != "resolved":
            if row.acknowledged_at is None:
                row.acknowledged_at, row.acknowledged_by = now, actor
            row.status, row.resolution, row.resolved_at, row.resolved_by, row.updated_at = "resolved", resolution.strip(), now, actor, now
            row.owner = row.owner or actor
            still_open = s.scalar(select(OfOperatorCase.case_id).where(
                OfOperatorCase.account_id == account_id, OfOperatorCase.fan_id == row.fan_id,
                OfOperatorCase.status.in_(OPEN), OfOperatorCase.case_id != row.case_id).limit(1))
            state = s.get(OfConversationState, (account_id, row.fan_id))
            if state is not None and not still_open and state.unresolved_attempt_id is None:
                state.requires_attention = False
        return _view(row, now)


def resolve_for_consent(s, account_id, fan_id, *, actor, consent_ref, now):
    """A recorded renewed consent (the audited way out of no_contact) resolves the stop-contact case with it."""
    s.execute(update(OfOperatorCase).where(
        OfOperatorCase.account_id == account_id, OfOperatorCase.fan_id == fan_id,
        OfOperatorCase.category == "stop_contact", OfOperatorCase.status.in_(OPEN)).values(
        status="resolved", resolution=f"Renewed consent recorded: {consent_ref}"[:2000], resolved_at=now,
        resolved_by=actor, updated_at=now))


def resolve_on_handback(s, account_id, fan_id, *, actor, now):
    """Hand-back to automation closes the identity / external-sender case (a person has answered); other cases keep their rules."""
    s.execute(update(OfOperatorCase).where(
        OfOperatorCase.account_id == account_id, OfOperatorCase.fan_id == fan_id,
        OfOperatorCase.category.in_(("identity", "external_sender")), OfOperatorCase.status.in_(OPEN)).values(
        status="resolved", resolution="Handed back to automation by the answering person", resolved_at=now,
        resolved_by=actor, updated_at=now))


def sweep_overdue(store, tenant_id, account_id, *, now=None):
    """Persist escalation for overdue open cases: one level per elapsed SLA interval. Returns the number escalated."""
    now = now or utcnow()
    count = 0
    with Session(store.engine) as s, s.begin():
        store._runtime(s, tenant_id, account_id)
        for row in s.scalars(select(OfOperatorCase).where(OfOperatorCase.account_id == account_id,
                OfOperatorCase.status.in_(OPEN), OfOperatorCase.due_at.is_not(None), OfOperatorCase.due_at < now)
                .with_for_update()).all():
            interval = SLA_SECONDS.get(row.category, SLA_SECONDS["other"])
            level = 1 + int((now - row.due_at).total_seconds() // interval)
            if level > row.escalation_level:
                row.escalation_level, row.escalated_at, row.updated_at = level, now, now
                count += 1
    return count
