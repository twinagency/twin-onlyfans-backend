"""Audited administrator activation commands. Each needs management permission and an exact account fingerprint.

* ``preview_existing`` / ``enable_existing``  enable AI replies for the intended existing conversations, in bounded,
  resumable, idempotent batches, preserving human takeovers, no-contact states and every explicit exclusion.
* ``set_future_conversations``               the explicit, visible setting for conversations that START later (default off).
* ``set_operating_limits``                   total send allowance, send interval and media switch. Used units are never
                                             reset and the allowance can never be set below what was consumed.

No command sends a message, verifies with a provider or changes an AI spending budget. Every per-fan enablement goes
through ``Store.control`` (the same revision-checked, audited path as the per-fan dashboard button).
"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from db.models import AccountAiConfig, Fan, OfConversationState, SkipList
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfDeliveryPolicyChange
from .activation_status import _selection, escalation_clause
from .launch_operations import _prior, _record, command_identity, digest
from .launch_operations_models import OfLaunchCommand  # noqa: F401 -- table registered
from .runtime_stop import STRONGER
from .store import Refused, utcnow

MAX_BATCH = 500
DEFAULT_BATCH = 200
SKIP_REASONS = ("no_contact", "human_takeover", "paused_by_staff", "asked_for_person", "muted", "restricted",
                "unresolved_delivery", "outside_scope", "changed_meanwhile", "refused_other")
_CONTROL_REFUSALS = {"unresolved delivery holds resumption": "unresolved_delivery",
                     "a stronger restriction holds": "restricted",
                     "control revision conflict": "changed_meanwhile",
                     "explicit hand-back is required": "human_takeover",
                     "replies are not paused": "changed_meanwhile"}


class Blocked(Refused):
    """Prerequisites are unmet. ``blockers`` are fixed codes the dashboard can render."""

    def __init__(self, blockers):
        super().__init__("activation prerequisites unmet: " + ",".join(blockers))
        self.blockers = list(blockers)


def fingerprint(s, runtime):
    """Account-level state a bulk command was decided against (not the live conversation counts)."""
    policy = s.get(OfDeliveryPolicy, runtime.account_id)
    connection = s.get(OfProviderConnection, runtime.account_id)
    config_scope = _selection(s.get(AccountAiConfig, runtime.account_id))
    value = {"binding": runtime.binding_id, "enabled": runtime.enabled,
             "policy": policy.revision if policy else 0, "connection": connection.revision if connection else 0,
             "future": bool(connection and connection.automate_new_conversations), "scope": config_scope[0]}
    return digest(value)


def _blockers(s, runtime, credential_reader):
    """Fixed codes for what stops an enablement now. Empty means the account may serve enabled chats."""
    from .activation_status import activation_status
    status = activation_status(s, runtime, credential_reader=credential_reader)
    out = []
    if not status["activated"]:
        out.append("account_not_activated")
    if status["stages"]["connection"]["state"] != "ok":
        out.append(status["stages"]["connection"]["code"])
    if status["stages"]["model"]["state"] != "ok" and status["stages"]["model"]["code"] != "budget_exhausted":
        out.append(status["stages"]["model"]["code"])
    replies = status["stages"]["replies"]["code"]
    if replies in ("delivery_disabled", "scope_invalid"):
        out.append(replies)
    return list(dict.fromkeys(out)), status


def preview_existing(s, runtime, *, credential_reader=None):
    """Exact counts of what enabling would do now. Writes nothing."""
    account = runtime.account_id
    state = OfConversationState
    selection, valid = _selection(s.get(AccountAiConfig, account))
    blockers, _ = _blockers(s, runtime, credential_reader)
    muted = select(Fan.fan_id).where(Fan.account_id == account, Fan.is_muted.is_(True))
    restricted = select(SkipList.fan_id).where(SkipList.account_id == account, SkipList.reason.in_(tuple(STRONGER)))
    candidate = and_(state.account_id == account, state.mode == "paused", state.revision == 0, state.takeover_state == "none")
    selected = None if selection is None or selection["mode"] == "all" else set(selection["fans"])

    def count(*clauses):
        return s.scalar(select(func.count()).select_from(state).where(state.account_id == account, *clauses)) or 0
    out = {"alreadyOn": count(state.mode == "automated", state.takeover_state == "none"),
           "noContact": count(state.mode == "no_contact"),
           "humanTakeover": count(state.mode == "human_controlled") + count(state.mode != "human_controlled", state.takeover_state != "none"),
           "askedForPerson": count(escalation_clause()),
           "pausedByStaff": count(state.mode == "paused", state.revision > 0) - count(escalation_clause()),
           "muted": count(candidate, state.fan_id.in_(muted)),
           "restricted": count(candidate, state.fan_id.in_(restricted)),
           "unresolvedDelivery": count(candidate, state.unresolved_attempt_id.is_not(None))}
    eligible_q = [candidate, ~state.fan_id.in_(muted), ~state.fan_id.in_(restricted), state.unresolved_attempt_id.is_(None)]
    total_eligible = count(*eligible_q)
    outside = 0
    if selected is not None:
        outside = count(*eligible_q, ~state.fan_id.in_(selected or {0}))
    out["outsideScope"] = outside
    out["wouldEnable"] = total_eligible - outside
    out["total"] = count()
    return {"previewVersion": "of-bulk-enable-preview.v1", "counts": out, "blockers": blockers,
            "scopeMode": selection["mode"] if selection else "all", "scopeValid": valid,
            "fingerprint": fingerprint(s, runtime)}


def _per_fan_key(command_key, fan_id):
    return hashlib.sha256(f"bulk-enable:{command_key}:{fan_id}".encode()).hexdigest()


def enable_existing(store, tenant, account, *, command_key, actor, reason, expected_fingerprint, after_fan=0,
                    batch=DEFAULT_BATCH, credential_reader=None):
    """One resumable batch. Call again with ``nextAfterFan`` until ``done``. Replaying a batch is harmless."""
    command_identity(command_key, actor, reason, expected_fingerprint)
    if type(after_fan) is not int or not 0 <= after_fan < 2**63 or type(batch) is not int or not 1 <= batch <= MAX_BATCH:
        raise Refused("invalid bulk enable batch")
    request_hash = digest(["bulk_enable", expected_fingerprint, actor, reason, after_fan, batch])
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        prior = _prior(s, runtime, command_key, "bulk_enable", request_hash)
        if prior is not None:
            return prior
        if fingerprint(s, runtime) != expected_fingerprint:
            raise Refused("account control state changed")
        blockers, _ = _blockers(s, runtime, credential_reader)
        if blockers:
            raise Blocked(blockers)
        selection, _valid = _selection(s.get(AccountAiConfig, account))
        selected = None if selection is None or selection["mode"] == "all" else set(selection["fans"])
        state = OfConversationState
        rows = s.execute(select(state.fan_id).where(
            state.account_id == account, state.mode == "paused", state.revision == 0, state.takeover_state == "none",
            state.fan_id > after_fan).order_by(state.fan_id).limit(batch + 1)).scalars().all()
        window, more = rows[:batch], len(rows) > batch
        muted = set(s.scalars(select(Fan.fan_id).where(Fan.account_id == account, Fan.fan_id.in_(window or [0]),
                                                       Fan.is_muted.is_(True))))
    skipped = {key: 0 for key in SKIP_REASONS}
    enabled = 0
    for fan in window:
        if selected is not None and fan not in selected:
            skipped["outside_scope"] += 1
        elif fan in muted:
            skipped["muted"] += 1
        else:
            try:
                store.control(tenant, account, fan, mode="automated", expected_revision=0,
                              command_key=_per_fan_key(command_key, fan), actor=actor,
                              reason="Bulk activation: " + reason[:480], action="resume_automation")
                enabled += 1
            except Refused as exc:
                skipped[_CONTROL_REFUSALS.get(str(exc), "refused_other")] += 1
    result = {"recorded": True, "enabled": enabled, "skipped": {k: v for k, v in skipped.items() if v},
              "checked": len(window), "done": not more, "nextAfterFan": str(window[-1]) if window and more else None,
              "commandKey": command_key}
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        _record(s, runtime, command_key, "bulk_enable", request_hash, actor, reason,
                {"afterFan": after_fan, "batch": batch}, result, utcnow())
    return result


def set_future_conversations(store, tenant, account, *, enabled, command_key, actor, reason, expected_fingerprint,
                             credential_reader=None):
    """Explicit on/off for conversations that begin AFTER this moment. Existing chats are untouched."""
    command_identity(command_key, actor, reason, expected_fingerprint)
    if type(enabled) is not bool:
        raise Refused("invalid future conversation setting")
    request_hash = digest(["future_conversations", enabled, expected_fingerprint, actor, reason])
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        prior = _prior(s, runtime, command_key, "future_conversations", request_hash)
        if prior is not None:
            return prior
        if fingerprint(s, runtime) != expected_fingerprint:
            raise Refused("account control state changed")
        connection = s.get(OfProviderConnection, account)
        if connection is None or connection.binding_id != runtime.binding_id:
            raise Refused("scope unavailable")
        before = {"enabled": bool(connection.automate_new_conversations), "startsAt": connection.automation_starts_at and
                  connection.automation_starts_at.isoformat()}
        now = utcnow()
        if enabled:
            blockers, _ = _blockers(s, runtime, credential_reader)
            if blockers:
                raise Blocked(blockers)
            # Only conversations whose first message arrives after this moment qualify (see Store._apply).
            if not connection.automate_new_conversations or connection.automation_starts_at is None:
                connection.automation_starts_at = now
            connection.automate_new_conversations = True
        else:
            connection.automate_new_conversations, connection.automation_starts_at = False, None
        connection.updated_at = now
        result = {"recorded": True, "enabled": enabled, "startsAt": connection.automation_starts_at and
                  connection.automation_starts_at.isoformat() + "Z", "commandKey": command_key}
        return _record(s, runtime, command_key, "future_conversations", request_hash, actor, reason, before, result, now)


def set_operating_limits(store, tenant, account, *, limit_units, throttle_seconds, media_enabled, command_key, actor,
                         reason, expected_fingerprint):
    """Deliberate commercial limits. Never resets used units, queue, holds or the send interval clock."""
    command_identity(command_key, actor, reason, expected_fingerprint)
    if (type(limit_units) is not int or not 1 <= limit_units <= 10000 or type(throttle_seconds) is not int
            or not 1 <= throttle_seconds <= 86400 or type(media_enabled) is not bool):
        raise Refused("invalid delivery limits")
    request_hash = digest(["operating_limits", limit_units, throttle_seconds, media_enabled, expected_fingerprint, actor, reason])
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        prior = _prior(s, runtime, command_key, "operating_limits", request_hash)
        if prior is not None:
            return prior
        if fingerprint(s, runtime) != expected_fingerprint:
            raise Refused("account control state changed")
        policy = s.get(OfDeliveryPolicy, account)
        if policy is None or policy.transport_kind != "native":
            raise Refused("delivery policy unavailable")
        if limit_units < policy.used_units:
            raise Refused("used allowance cannot be reset")
        from datetime import timedelta
        now = utcnow()
        before = {name: getattr(policy, name) for name in ("enabled", "limit_units", "throttle_seconds", "transport_kind", "media_enabled")}
        if throttle_seconds > policy.throttle_seconds and policy.next_dispatch_at is not None:
            policy.next_dispatch_at = max(policy.next_dispatch_at, now + timedelta(seconds=throttle_seconds))
        policy.revision += 1
        policy.limit_units, policy.throttle_seconds, policy.media_enabled = limit_units, throttle_seconds, media_enabled
        after = {name: getattr(policy, name) for name in before}
        s.add(OfDeliveryPolicyChange(account_id=account, revision=policy.revision, actor=actor,
                                     reason="Operating limits: " + reason[:480],
                                     before_json=json.dumps(before, sort_keys=True), after_json=json.dumps(after, sort_keys=True),
                                     created_at=now))
        result = {"recorded": True, "limitUnits": limit_units, "usedUnits": policy.used_units,
                  "throttleSeconds": throttle_seconds, "mediaEnabled": media_enabled, "commandKey": command_key}
        return _record(s, runtime, command_key, "operating_limits", request_hash, actor, reason, before, result, now)


def read_activation(store, tenant, account, *, credential_reader=None, runner_path=None):
    """The status, the exact preview of "enable existing chats" and the fingerprint commands must quote."""
    from .activation_status import activation_status
    from .launch_operations import read_scope
    with Session(store.engine) as s, s.begin():
        if s.bind.dialect.name == "postgresql":
            s.connection(execution_options={"isolation_level": "REPEATABLE READ"}).exec_driver_sql("SET TRANSACTION READ ONLY")
        runtime = read_scope(store, s, tenant, account)
        s.info["onlyfans_read_snapshot"] = True   # the credential reader must not take row locks in a read-only snapshot
        status = activation_status(s, runtime, credential_reader=credential_reader, runner_path=runner_path)
        preview = preview_existing(s, runtime, credential_reader=credential_reader)
        return {**status, "bulkEnable": preview, "fingerprint": preview["fingerprint"]}


def activate_account(store, tenant, account, *, limit_units, throttle_seconds, media_enabled, expected_revision, command_key, actor,
                     credential_reader=None):
    """The explicit administrator activation of a verified account with deliberate commercial limits.

    Needs a verified, unexpired connection AND this account's own usable model credential. It enables outgoing for
    the account only; no conversation is enabled by it and the future-conversations setting stays off."""
    from onlyfans_read.agency_scope import actor_allowed
    from .connections import change_connection
    from .launch_operations import read_scope
    if not actor_allowed(actor):
        raise Refused("management permission required")
    with Session(store.engine) as s, s.begin():
        runtime = read_scope(store, s, tenant, account)
        blockers, _ = _blockers(s, runtime, credential_reader)
    blockers = [b for b in blockers if b not in ("account_not_activated", "delivery_disabled")]
    if blockers:
        raise Blocked(blockers)
    revision = change_connection(store, tenant, account, action="activate", expected_revision=expected_revision,
                                 command_key=command_key, actor=actor, payload={"limitUnits": limit_units,
                                 "throttleSeconds": throttle_seconds, "mediaEnabled": media_enabled})
    return {"recorded": True, "revision": revision, "commandKey": command_key}
