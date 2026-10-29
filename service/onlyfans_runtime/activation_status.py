"""Read-only activation status for one OnlyFans account: what an agency administrator must see.

Four stages, each with a state and the exact reason when it is not "ok":

* ``connection``  the OnlyFans session is saved, verified and unexpired;
* ``model``       this account's OWN model credential is usable and today's model budget is not spent;
* ``runtime``     the FASTT sender process for this account is supervised and running;
* ``replies``     conversations can actually receive replies (account activated, allowance left, scope,
                  at least one conversation enabled), with human takeover and escalation counts.

Nothing here writes, verifies against a provider, renews a lease or enables anything. Every figure comes from the
rows the runtime itself enforces (``of_runtime``, ``of_delivery_policy``, ``of_provider_connection``,
``account_ai_config``, ``of_conversation_state``) or from the supervisor health file, so the dashboard cannot say
"ready" while the sender would stay silent. A fact that cannot be proven is ``unknown``, never ``ok``.
"""
from __future__ import annotations

import json
import os
from datetime import timedelta, timezone

from sqlalchemy import and_, exists, func, select

from db.models import AccountAiConfig, AutomationRun, GrokCall, GrokDailyCost, OfConversationState
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_delivery_models import OfDeliveryPolicy
from db.onlyfans_models import OfControlAudit
from .store import utcnow

VERSION = "of-activation-status.v1"
IDENTITY_ACTOR = "identity-guard"

# Supervisor hold reasons (native_pilot.supervisor) in the words an agency administrator can act on.
RUNTIME_REASONS = {
    "runtime-missing": ("account_not_registered", "The account has no runtime record yet.", "connect"),
    "runtime-disabled": ("account_not_activated", "The account has not been activated by an administrator.", "activate"),
    "binding-scope": ("binding_scope", "The account is bound to a different creator record.", "support"),
    "binding-revoked": ("binding_revoked", "The creator binding was revoked.", "support"),
    "policy-disabled": ("delivery_disabled", "Outgoing messages are switched off for this account.", "activate"),
    "connection-missing": ("not_connected", "No OnlyFans session is saved.", "connect"),
    "connection-scope": ("connection_scope", "The saved session belongs to a different binding.", "connect"),
    "connection-not-verified": ("connection_unverified", "The saved session has not been verified.", "connect"),
    "connection-check-failing": ("connection_check_failing", "The latest session check failed.", "connect"),
    "connection-expired": ("connection_expired", "The session lease expired; sign in again.", "connect"),
    "connection-owner-mismatch": ("connection_owner_mismatch", "The session belongs to another OnlyFans owner.", "connect"),
    "not-in-scope": ("not_in_deployment_scope", "The account is not in the deployment scope (ONLYFANS_FASTT_SCOPE).", "support"),
    "handle-missing": ("handle_missing", "The account has no OnlyFans handle saved.", "connect"),
    "handle-invalid": ("handle_invalid", "The saved OnlyFans handle is not valid.", "connect"),
    "content-mode-conflict": ("content_mode_conflict", "The content mode settings conflict.", "support"),
    "crash-loop": ("sender_crash_loop", "The sender stopped repeatedly and is held until its configuration changes.", "support"),
    "too-many-accounts": ("sender_capacity", "The sender is at its account capacity.", "support"),
}
MANIFEST_REASONS = {
    "no-approved-items": "No enabled fixed-price paid item with vault media is approved yet.",
    "not-fixed-paid": "An enabled catalogue item is not fixed-price paid.",
    "invalid-row": "An enabled catalogue item is malformed.",
    "invalid-media": "An enabled catalogue item has invalid media.",
    "price-out-of-range": "An enabled catalogue item has a price outside the allowed range.",
    "media-not-in-vault": "An enabled catalogue item uses media that is not in the vault.",
    "too-many-items": "Too many enabled catalogue items.",
}
CREDENTIAL_REASONS = {
    "missing": ("model_credential_missing", "This account has no model API key saved. Add its own key and model."),
    "invalid": ("model_credential_invalid", "The saved model credential could not be read. Save it again."),
    "account_mismatch": ("model_credential_wrong_account", "The model credential belongs to another account."),
    "model_mismatch": ("model_credential_model_mismatch", "The saved key is for a different model than the one selected in settings."),
    "expired": ("model_credential_expired", "The model credential expired with the connection lease."),
}


PURPOSE_LABELS = {"ai_chatter": "Chat replies", "gen_info": "Fan memory learner", "describe_media": "Media descriptions"}


def _stage(state, code=None, reason=None, action=None, **extra):
    return {"state": state, "code": code, "reason": reason, "action": action, **extra}


def _z(value):
    return value.isoformat() + "Z" if value is not None else None


def _connection_stage(connection, runtime, now):
    if connection is None:
        return _stage("blocked", "not_connected", "No OnlyFans session is saved for this account.", "connect",
                      expiresAt=None, verifiedAt=None, revision=0)
    base = {"expiresAt": _z(connection.expires_at), "verifiedAt": _z(connection.verified_at), "revision": connection.revision}
    if connection.binding_id != runtime.binding_id:
        return _stage("blocked", "connection_scope", "The saved session belongs to a different creator binding.", "connect", **base)
    if connection.status == "paused":
        return _stage("blocked", "connection_paused", "Outgoing was paused for this connection.", "connect", **base)
    if connection.status == "pending":
        return _stage("blocked", "connection_unverified", "The saved session is waiting for verification.", "connect", **base)
    if connection.status in ("failed", "expired") or connection.last_error is not None:
        return _stage("blocked", "connection_check_failing",
                      "The latest session check failed" + (f" ({connection.last_error})." if connection.last_error else "."),
                      "connect", **base)
    if connection.verified_at is None:
        return _stage("blocked", "connection_unverified", "The saved session has not been verified.", "connect", **base)
    if connection.expires_at <= now:
        return _stage("blocked", "connection_expired", "The session lease expired. Sign in again to renew it.", "connect", **base)
    hours = (connection.expires_at - now).total_seconds() / 3600
    return _stage("ok", None, None, None, hoursLeft=round(hours, 1), **base)


def _budget(s, account, config, now, policy):
    """Today's model budget for the account. Shown, never changed."""
    cap = config.daily_cost_cap_cents if config is not None else None
    day = now.date().isoformat()
    spent = s.scalar(select(func.coalesce(func.sum(GrokDailyCost.cost_cents), 0)).where(
        GrokDailyCost.account_id == account, GrokDailyCost.day == day)) or 0
    since = now - timedelta(days=3)
    # Observed cost per chat reply (ledger units are hundredths of a cent), and what else spends the same cap.
    chat = s.execute(select(func.count(), func.coalesce(func.avg(GrokCall.cost_cents), 0)).where(
        GrokCall.account_id == account, GrokCall.purpose == "ai_chatter", GrokCall.status == "done",
        GrokCall.called_at >= since)).one()
    by_purpose = {row[0]: int(row[1] or 0) for row in s.execute(select(GrokCall.purpose, func.coalesce(func.sum(GrokCall.cost_cents), 0)).where(
        GrokCall.account_id == account, GrokCall.called_at >= now.replace(hour=0, minute=0, second=0, microsecond=0)
    ).group_by(GrokCall.purpose))}
    chat_today = by_purpose.get("ai_chatter", 0)
    other_today = max(0, sum(by_purpose.values()) - chat_today)
    total_today = sum(by_purpose.values())
    spend = [{"purpose": purpose, "label": PURPOSE_LABELS.get(purpose, purpose), "units": units,
              "share": round(units / total_today, 3) if total_today else 0.0}
             for purpose, units in sorted(by_purpose.items(), key=lambda item: -item[1]) if units > 0]
    breakdown = ", ".join(f"{row['label']} {round(row['share'] * 100)}%" for row in spend)
    result = {"dailyCostCapCents": cap, "spentTodayUnits": int(spent), "capUnits": cap * 100 if isinstance(cap, int) else None,
              "resetsAt": _z((now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)),
              "chatSpendTodayUnits": chat_today, "otherSpendTodayUnits": other_today, "spendByPurpose": spend,
              "sampleReplies": int(chat[0]), "avgUnitsPerReply": round(float(chat[1]), 1) if chat[0] else None,
              "state": "unset", "flag": None}
    if not isinstance(cap, int) or cap <= 0:
        result["flag"] = "No daily model budget is set, so the sender cannot call the model."
        return result
    if spent >= cap * 100:
        result.update(state="exhausted", flag="Today's model budget is spent, so no reply can be written until it resets at 00:00 UTC."
                      + (f" Spend by purpose: {breakdown}." if breakdown else ""))
        return result
    result["state"] = "ok"
    remaining_allowance = max(0, (policy.limit_units - policy.used_units)) if policy else 0
    capacity = 86400 // policy.throttle_seconds if policy and policy.throttle_seconds else 0
    workload = min(remaining_allowance, capacity)
    result["plannedRepliesPerDay"] = workload
    if chat[0] and workload and chat[1] and workload * float(chat[1]) > cap * 100:
        result.update(state="too_small", flag=(
            f"The configured workload (up to {workload} replies a day) would cost about "
            f"{round(workload * float(chat[1]) / 100, 1)} cents at the observed cost per reply, above the {cap} cent daily budget."))
    elif cap and other_today * 2 >= cap * 100 and chat_today * 4 < other_today:
        result.update(state="shared", flag=(
            "Most of today's budget was used by background work, not by chat replies. " + (f"Spend by purpose: {breakdown}." if breakdown else "")))
    return result


def provider_errors(s, account, now):
    """What the model provider has been answering this account's calls with (observed, not probed)."""
    since = now - timedelta(hours=24)
    rows = s.execute(select(GrokCall.error_text, func.count(), func.max(GrokCall.called_at)).where(
        GrokCall.account_id == account, GrokCall.status == "error", GrokCall.called_at >= since,
        GrokCall.error_text.is_not(None)).group_by(GrokCall.error_text).limit(500)).all()
    out = {"credit": [0, None], "rejected": [0, None], "rate_limited": [0, None]}
    for text_, count, last in rows:
        head = (text_ or "")[:60]
        kind = ("credit" if "HTTP 402" in head else "rejected" if "HTTP 401" in head or "HTTP 403" in head
                else "rate_limited" if "HTTP 429" in head else None)
        if kind:
            out[kind][0] += count
            out[kind][1] = max(filter(None, [out[kind][1], last])) if last else out[kind][1]
    recent = out["credit"][1] is not None and now - out["credit"][1] <= timedelta(minutes=30)
    return {"creditErrors24h": out["credit"][0], "lastCreditErrorAt": _z(out["credit"][1]), "creditErrorsRecent": recent,
            "rejectedKey24h": out["rejected"][0], "lastRejectedAt": _z(out["rejected"][1]),
            "rateLimited24h": out["rate_limited"][0]}


def recent_failures(s, account, now, limit=6):
    """Why chat replies failed in the last 24 hours (fixed reasons recorded by the sender)."""
    rows = s.execute(select(AutomationRun.error_text, func.count(), func.max(AutomationRun.started_at)).where(
        AutomationRun.account_id == account, AutomationRun.kind == "ai_chatter", AutomationRun.status == "error",
        AutomationRun.started_at >= now - timedelta(hours=24), AutomationRun.error_text.is_not(None)
    ).group_by(AutomationRun.error_text).order_by(func.count().desc()).limit(limit)).all()
    return [{"reason": (reason or "")[:80], "count": count, "lastAt": _z(last)} for reason, count, last in rows]


def _model_stage(s, runtime, config, connection, credential_reader, now, budget, provider):
    from .launch_personality.policy import readiness as ai_readiness
    credentials = None
    if credential_reader is not None:
        try:
            credentials = credential_reader(s, runtime.account_id, runtime.binding_id)
        except Exception:
            credentials = None  # Unreadable credentials are reported as missing, never as ready.
    cols = {c.name: getattr(config, c.name) for c in config.__table__.columns} if config is not None else {}
    ai = ai_readiness(cols, credentials, now=now.replace(tzinfo=timezone.utc))
    extra = {"modelId": ai["model"].get("modelId"), "provider": ai["model"].get("provider")}
    if config is None or ai["model"]["state"] != "ready":
        return _stage("blocked", "model_not_selected", "No valid reply model is selected in this creator's settings.", "settings", **extra)
    cred_state = ai["credential"]["state"]
    if cred_state != "ready":
        code, reason = CREDENTIAL_REASONS.get(cred_state, ("model_credential_missing", "The model credential is not usable."))
        if connection is None or connection.status != "verified" or connection.expires_at <= now:
            reason += " It is stored with the connection, which is not currently verified."
        return _stage("blocked", code, reason, "model_credential", **extra)
    if provider["creditErrorsRecent"]:
        return _stage("blocked", "provider_no_credit", (
            f"The model provider refused recent requests for lack of credit (HTTP 402, {provider['creditErrors24h']} in the last 24 hours, "
            f"last at {provider['lastCreditErrorAt']}). Top up the provider account for this key."), "model_credential", **extra)
    if budget["state"] == "unset":
        return _stage("blocked", "budget_unset", budget["flag"], "settings", **extra)
    if budget["state"] == "exhausted":
        return _stage("blocked", "budget_exhausted", budget["flag"], None, **extra)
    return _stage("ok", None, None, None, **extra)


def _runtime_stage(account, runtime, runner_path, now):
    from .runner_state import runner_state
    state = runner_state(account, path=runner_path, now=now)
    base = {"runnerState": state["state"], "observedAt": state["observedAt"], "restarts": state["restarts"]}
    if state["state"] == "running":
        return _stage("ok", None, None, None, **base)
    if state["state"] == "restarting":
        return _stage("warn", "sender_restarting", "The sender is restarting after an exit.", None, **base)
    if state["state"] == "stopping":
        return _stage("warn", "sender_stopping", "The sender is stopping.", None, **base)
    if state["state"] == "not_supervised":
        return _stage("blocked", "sender_not_supervised", "The sender process is not running for this account. It starts once the account is activated and in scope.", "activate", **base)
    if state["state"] == "held":
        raw = state.get("reason") or ""
        if raw.startswith("manifest:"):
            detail = MANIFEST_REASONS.get(raw[9:], "The catalogue manifest could not be built.")
            return _stage("blocked", "sender_held_catalogue", "The sender is held: " + detail, "content", holdReason=raw, **base)
        code, reason, action = RUNTIME_REASONS.get(raw, ("sender_held", f"The sender is held ({raw or 'no reason recorded'}).", "support"))
        return _stage("blocked", code, "The sender is held: " + reason, action, holdReason=raw or None, **base)
    reason = {"status_stale": "The sender health report is stale, so the sender may not be running.",
              "status_unavailable": "The sender health report is not available to this service.",
              "status_invalid": "The sender health report could not be read."}.get(state.get("reason"), "Sender health is unknown.")
    return _stage("unknown", "sender_unknown", reason, "support", **base)


def _selection(config):
    from .fastt_scope import KEY, normalize
    raw = getattr(config, "ai_chatter_config_json", None) if config is not None else None
    if not raw:
        return None, True
    try:
        value = json.loads(raw)
        return (normalize(value[KEY]), True) if isinstance(value, dict) and value.get(KEY) is not None else (None, True)
    except Exception:
        return None, False


def escalation_clause():
    """Conversations the engine paused because the fan asked for a person, still paused since."""
    return exists().where(OfControlAudit.account_id == OfConversationState.account_id,
        OfControlAudit.fan_id == OfConversationState.fan_id, OfControlAudit.action == "pause_automation",
        OfControlAudit.actor == IDENTITY_ACTOR, OfControlAudit.revision == OfConversationState.revision,
        OfConversationState.mode == "paused")


def conversation_counts(s, account):
    state = OfConversationState
    rows = s.execute(select(state.mode, state.takeover_state, (state.revision > 0), func.count()).where(
        state.account_id == account).group_by(state.mode, state.takeover_state, (state.revision > 0))).all()
    counts = {"total": 0, "automated": 0, "enabledNow": 0, "paused": 0, "pausedByStaff": 0, "noContact": 0,
              "humanControlled": 0, "takeoverRequested": 0, "notYetEnabled": 0}
    for mode, takeover, touched, count in rows:
        counts["total"] += count
        if takeover == "requested":
            counts["takeoverRequested"] += count
        if mode == "automated":
            counts["automated"] += count
            if takeover == "none":
                counts["enabledNow"] += count
        elif mode == "paused":
            counts["paused"] += count
            counts["pausedByStaff" if touched else "notYetEnabled"] += count
        elif mode == "no_contact":
            counts["noContact"] += count
        elif mode == "human_controlled":
            counts["humanControlled"] += count
    needs = s.scalar(select(func.count()).select_from(state).where(state.account_id == account, escalation_clause())) or 0
    unresolved = s.scalar(select(func.count()).select_from(state).where(
        state.account_id == account, state.unresolved_attempt_id.is_not(None))) or 0
    counts.update(needsPerson=needs, unresolvedDelivery=unresolved)
    return counts


def recent_escalations(s, account, limit=10):
    rows = s.execute(select(OfControlAudit.fan_id, OfControlAudit.created_at).join(OfConversationState, and_(
        OfConversationState.account_id == OfControlAudit.account_id, OfConversationState.fan_id == OfControlAudit.fan_id,
        OfConversationState.revision == OfControlAudit.revision)).where(
        OfControlAudit.account_id == account, OfControlAudit.action == "pause_automation",
        OfControlAudit.actor == IDENTITY_ACTOR, OfConversationState.mode == "paused"
    ).order_by(OfControlAudit.created_at.desc()).limit(limit)).all()
    return [{"fanId": str(fan), "at": _z(at)} for fan, at in rows]


def _replies_stage(runtime, connection, policy, selection, selection_valid, counts, stages):
    if not runtime.enabled:
        return _stage("blocked", "account_not_activated",
                      "The account has not been activated. Nothing is sent until an administrator activates it.", "activate")
    if policy is None or not policy.enabled or policy.transport_kind != "native":
        return _stage("blocked", "delivery_disabled", "Outgoing messages are switched off for this account.", "activate")
    if policy.used_units >= policy.limit_units:
        return _stage("blocked", "allowance_exhausted",
                      f"The send allowance is used up ({policy.used_units} of {policy.limit_units}). Raise the total allowance to continue.", "limits")
    if not selection_valid:
        return _stage("blocked", "scope_invalid", "The saved fan scope needs review before replies can be served.", "settings")
    for name in ("connection", "model", "runtime"):
        if stages[name]["state"] in ("blocked",):
            return _stage("blocked", "prerequisite_" + name, f"Waiting on the {name} check above.", None)
    if counts["enabledNow"] == 0:
        return _stage("blocked", "no_enabled_conversations",
                      "No conversation is enabled for AI replies. Enable existing chats or enable individual fans.", "enable_existing")
    if stages["runtime"]["state"] == "unknown":
        return _stage("unknown", "sender_unknown", "Sender health is unknown, so replies cannot be confirmed.", "support")
    return _stage("ok", None, None, None)


def activation_status(s, runtime, *, credential_reader=None, runner_path=None, now=None):
    """The whole activation picture for one account, from a single read-only transaction."""
    from .activation import deployment_scope
    now = now or utcnow()
    account = runtime.account_id
    connection = s.get(OfProviderConnection, account)
    policy = s.get(OfDeliveryPolicy, account)
    config = s.get(AccountAiConfig, account)
    selection, selection_valid = _selection(config)
    budget = _budget(s, account, config, now, policy)
    stages = {"connection": _connection_stage(connection, runtime, now)}
    provider = provider_errors(s, account, now)
    stages["model"] = _model_stage(s, runtime, config, connection, credential_reader, now, budget, provider)
    stages["runtime"] = _runtime_stage(account, runtime, runner_path, now)
    counts = conversation_counts(s, account)
    stages["replies"] = _replies_stage(runtime, connection, policy, selection, selection_valid, counts, stages)
    scope = deployment_scope(account)
    from .store import fan_current_override
    overrides = sorted(fan_current_override(account))
    limit = policy.limit_units if policy else 0
    used = policy.used_units if policy else 0
    warnings = []
    if overrides:
        warnings.append({"code": "pilot_fan_override", "text": f"A temporary single-fan history override is set for {len(overrides)} fan(s) on this deployment."})
    if provider["creditErrors24h"] and not provider["creditErrorsRecent"]:
        warnings.append({"code": "provider_credit_errors", "text": (
            f"The model provider refused {provider['creditErrors24h']} requests for lack of credit in the last 24 hours (last at {provider['lastCreditErrorAt']}).")})
    if provider["rejectedKey24h"]:
        warnings.append({"code": "provider_rejected_key", "text": f"The model provider rejected this key {provider['rejectedKey24h']} times in the last 24 hours."})
    if budget["flag"] and budget["state"] in ("too_small", "shared", "exhausted"):
        warnings.append({"code": "budget_" + budget["state"], "text": budget["flag"]})
    if policy is not None and limit and used >= limit * 0.9 and used < limit:
        warnings.append({"code": "allowance_low", "text": f"Only {limit - used} sends remain in the allowance."})
    return {
        "statusVersion": VERSION, "observedAt": _z(now), "accountId": account,
        "activated": bool(runtime.enabled), "ready": all(stages[k]["state"] == "ok" for k in stages),
        "stages": stages,
        "allowance": {"limitUnits": limit, "usedUnits": used, "remainingUnits": max(0, limit - used),
                      "throttleSeconds": policy.throttle_seconds if policy else None,
                      "mediaEnabled": bool(policy and policy.media_enabled), "policyRevision": policy.revision if policy else 0,
                      "nextSendAt": _z(policy.next_dispatch_at) if policy and policy.next_dispatch_at else None,
                      "state": "none" if policy is None else "exhausted" if used >= limit else "ok"},
        "budget": budget, "provider": provider, "recentFailures": recent_failures(s, account, now),
        "scope": {**scope, "selection": selection, "selectionValid": selection_valid,
                  "pilotFanOverride": [str(f) for f in overrides]},
        "conversations": counts,
        "escalations": {"needsPerson": counts["needsPerson"], "takeoverRequested": counts["takeoverRequested"],
                        "humanControlled": counts["humanControlled"], "unresolvedDelivery": counts["unresolvedDelivery"],
                        "recent": recent_escalations(s, account) if counts["needsPerson"] else []},
        "futureConversations": {"enabled": bool(connection and connection.automate_new_conversations and connection.automation_starts_at),
                                "startsAt": _z(connection.automation_starts_at) if connection else None, "default": False},
        "warnings": warnings,
    }
