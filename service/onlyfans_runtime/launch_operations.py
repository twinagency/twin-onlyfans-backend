"""Scoped operator launch commands and stored-state projections.

No provider reads or sends occur here. Runtime's existing account lock serializes
controls with admission and the final one-use dispatch marker.
"""
from datetime import datetime, timedelta
import hashlib
import json
import re

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from db.models import ContentOffer, Fan, Message, OfCreatorBinding, Transaction
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfNativePlan, OfTextIntent
from db.onlyfans_ledger_models import OfLedgerEvidence, OfLedgerExclusion, OfLedgerScan
from db.onlyfans_models import OfRuntime, OfWork
from onlyfans_read.agency_scope import actor_allowed
from .launch_operations_models import OfLaunchCommand, OfLedgerResolution, OfLaunchWorkerHealth
from .native_context import canonical
from .store import Refused, utcnow


def z(value):
    return value.isoformat() + "Z" if value is not None else None


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def command_identity(command_key, actor, reason, expected_fingerprint):
    if (type(command_key) is not str or re.fullmatch(r"[a-f0-9]{64}", command_key) is None
            or not actor_allowed(actor) or type(reason) is not str or not reason.strip()
            or len(reason) > 512 or "\x00" in reason or type(expected_fingerprint) is not str
            or re.fullmatch(r"[a-f0-9]{64}", expected_fingerprint) is None):
        raise Refused("invalid launch command")


def read_scope(store, s, tenant, account):
    """Deployment readers retain the existing explicit deployment grant."""
    if tenant is None:
        runtime = s.get(OfRuntime, account)
        binding = s.get(OfCreatorBinding, runtime.binding_id) if runtime else None
        if binding is None:
            raise Refused("scope unavailable")
        tenant = binding.tenant_id
    return store._runtime(s, tenant, account)


def _prior(s, runtime, command_key, kind, request_hash):
    prior = s.get(OfLaunchCommand, (runtime.account_id, command_key))
    if prior is None:
        return None
    if prior.binding_id != runtime.binding_id or prior.kind != kind or prior.request_hash != request_hash:
        raise Refused("launch command conflict")
    return json.loads(prior.result_json)


def _record(s, runtime, command_key, kind, request_hash, actor, reason, before, result, now):
    row = OfLaunchCommand(account_id=runtime.account_id, command_key=command_key,
        binding_id=runtime.binding_id, kind=kind, request_hash=request_hash, actor=actor,
        reason=reason, before_json=canonical(before), result_json=canonical(result), created_at=now)
    s.add(row)
    s.flush()
    return result


def account_state(s, runtime):
    unresolved = s.scalar(select(func.count()).select_from(OfTextIntent).where(
        OfTextIntent.account_id == runtime.account_id, OfTextIntent.status.in_(("dispatching", "unknown"))))
    # Commands are immutable and serialized by the runtime lock. Counting them
    # remains monotonic even when several commands have identical timestamps.
    revision = s.scalar(select(func.count()).select_from(OfLaunchCommand).where(
        OfLaunchCommand.account_id == runtime.account_id,
        OfLaunchCommand.binding_id == runtime.binding_id, OfLaunchCommand.kind == "account_control"))
    value = {"enabled": runtime.enabled, "bindingId": runtime.binding_id,
        "revision": revision, "unresolvedOutgoing": unresolved}
    return {**value, "fingerprint": digest(value)}


def control_account(store, tenant, account, *, action, command_key, actor, reason, expected_fingerprint,
                    credential_reader=None):
    command_identity(command_key, actor, reason, expected_fingerprint)
    if action not in {"pause", "emergency_stop", "resume"}:
        raise Refused("invalid account control action")
    request_hash = digest([action, expected_fingerprint, actor, reason])
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        prior = _prior(s, runtime, command_key, "account_control", request_hash)
        if prior is not None:
            return prior
        before = account_state(s, runtime)
        if before["fingerprint"] != expected_fingerprint:
            raise Refused("account control state changed")
        now = utcnow()
        if action == "resume":
            # Resuming permits only new, freshly validated work. Cancelled old
            # work stays cancelled; no contact/takeover settings are modified.
            connection = s.get(OfProviderConnection, account)
            policy = s.get(OfDeliveryPolicy, account)
            if (before["unresolvedOutgoing"] or connection is None
                    or connection.binding_id != runtime.binding_id or connection.status != "verified"
                    or connection.verified_at is None or connection.expires_at <= now
                    or policy is None or not policy.enabled or policy.limit_units <= policy.used_units):
                raise Refused("account resume prerequisites unavailable")
            if not _readiness(s, runtime, credential_reader=credential_reader)["readyToEnable"]:
                raise Refused("account readiness prerequisites unavailable")
            runtime.enabled = True
        else:
            runtime.enabled = False
            # Invalidate planner claims and waiting work in the same transaction.
            for work in s.scalars(select(OfWork).where(OfWork.account_id == account,
                    OfWork.status.in_(("waiting", "claimed")))):
                work.generation += 1
                work.status, work.lease_token, work.lease_until = "blocked", None, None
            for plan in s.scalars(select(OfNativePlan).where(OfNativePlan.account_id == account,
                    OfNativePlan.status.in_(("generating", "ready")))):
                plan.status, plan.reason = "stale", "account_stopped"
            policy = s.get(OfDeliveryPolicy, account)
            for intent in s.scalars(select(OfTextIntent).where(OfTextIntent.account_id == account,
                    OfTextIntent.status == "queued")):
                intent.status, intent.reason, intent.updated_at = "cancelled", "account_stopped", now
                if policy is not None:
                    policy.queued_count -= 1
            from .launch_engagement.service import invalidate
            invalidate(s, runtime, reason="pause", actor=actor, now=now)
            from db.onlyfans_campaign_models import OfCampaign, OfCampaignChange
            for campaign in s.scalars(select(OfCampaign).where(OfCampaign.account_id == account,
                    OfCampaign.status.in_(("scheduled", "running")))):
                previous = {"status": campaign.status, "revision": campaign.revision}
                campaign.status, campaign.revision, campaign.updated_at = "paused", campaign.revision + 1, now
                s.add(OfCampaignChange(account_id=account, campaign_id=campaign.campaign_id,
                    revision=campaign.revision, command_key=digest([command_key, campaign.campaign_id]),
                    command_hash=request_hash, action="pause", actor=actor,
                    before_json=canonical(previous), after_json=canonical({"status": "paused", "revision": campaign.revision}),
                    created_at=now))
        result = {"recorded": True, "action": action, "enabled": runtime.enabled,
            "revision": before["revision"] + 1, "unresolvedOutgoing": before["unresolvedOutgoing"],
            "oldWorkResumed": False, "commandKey": command_key}
        return _record(s, runtime, command_key, "account_control", request_hash, actor, reason, before, result, now)


def _ledger_core(s, runtime, *, now=None):
    scan = s.get(OfLedgerScan, runtime.account_id)
    current = scan is not None and scan.binding_id == runtime.binding_id
    binding = s.get(OfCreatorBinding, runtime.binding_id)
    now = now or utcnow()
    connection = s.get(OfProviderConnection, runtime.account_id)
    from .ledger import _current, coverage_proven
    from .launch_ledger.dashboard import ERRORS, fingerprint
    connection_current = bool(current and _current(runtime, connection, scan, now))
    conflicts = select(OfLedgerEvidence.evidence_hash).where(OfLedgerEvidence.account_id == runtime.account_id,
        OfLedgerEvidence.recorded_at >= binding.created_at,
        OfLedgerEvidence.outcome == "conflict", ~select(OfLedgerResolution.evidence_hash).where(
            OfLedgerResolution.account_id == OfLedgerEvidence.account_id,
            OfLedgerResolution.evidence_hash == OfLedgerEvidence.evidence_hash,
            OfLedgerResolution.binding_id == runtime.binding_id).exists())
    conflict_count = s.scalar(select(func.count()).select_from(conflicts.subquery()))
    unlinked_all = select(func.count()).select_from(Transaction).where(Transaction.account_id == runtime.account_id,
        Transaction.source == "ledger", Transaction.kind == "ppv_message", Transaction.message_id.is_(None),
        Transaction.occurred_at >= binding.created_at,
        Transaction.status.in_(("pending", "cleared", "refund_pending")))
    # An audited exclusion stops one unlinked purchase from holding the whole account. It stays
    # visible here, counted separately, and keeps holding its own fan (see commerce.held).
    is_excluded = select(OfLedgerExclusion.id).where(OfLedgerExclusion.account_id == Transaction.account_id,
        OfLedgerExclusion.transaction_id == Transaction.id,
        OfLedgerExclusion.binding_id == runtime.binding_id).exists()
    unlinked = s.scalar(unlinked_all.where(~is_excluded))
    excluded = s.scalar(unlinked_all.where(is_excluded))
    from .commerce import held
    value = {"bindingCurrent": current, "status": scan.status if current else "not_started",
        "windowStart": z(scan.window_start) if current else None, "windowEnd": z(scan.window_end) if current else None,
        "pageOffset": scan.page_offset if current else None,
        "lastCompleteAt": z(scan.last_complete_at) if current else None,
        "lastFullAt": z(scan.last_full_at) if current else None,
        "leaseUntil": z(scan.lease_until) if current else None,
        "activeLease": bool(current and scan.lease_token and scan.lease_until and scan.lease_until > now),
        "nextPollAt": z(scan.next_poll_at) if current else None,
        "lastError": (scan.last_error if scan.last_error in ERRORS else "unknown_error")
            if current and scan.last_error is not None else None,
        "updatedAt": z(scan.updated_at) if current else None,
        "fresh": bool(connection_current and coverage_proven(scan)
            and timedelta(0) <= now - scan.last_complete_at <= timedelta(minutes=15)),
        "unresolvedTransactions": unlinked, "excludedTransactions": excluded, "conflictingEvidence": conflict_count, "commerceHold": bool(held(s, runtime.account_id))}
    return {**value, "fingerprint": digest({"binding": runtime.binding_id,
        "scan": fingerprint(runtime, connection, scan if current else None),
        "conflicts": conflict_count, "unlinked": unlinked, "excluded": excluded})}


def ledger_status(store, tenant, account, *, cursor=None, limit=25):
    from .launch_pagination import decode, encode
    if type(limit) is not int or not 1 <= limit <= 100:
        raise Refused("invalid ledger page limit")
    with Session(store.engine) as s, s.begin():
        runtime = read_scope(store, s, tenant, account)
        position = decode(cursor, "ledger-evidence", account, runtime.binding_id)
        binding = s.get(OfCreatorBinding, runtime.binding_id)
        query = select(OfLedgerEvidence).where(OfLedgerEvidence.account_id == account,
            OfLedgerEvidence.recorded_at >= binding.created_at)
        if position is not None:
            if (type(position) is not list or len(position) != 2 or type(position[0]) is not str
                    or type(position[1]) is not str or re.fullmatch(r"[a-f0-9]{64}", position[1]) is None):
                raise Refused("invalid ledger cursor")
            try:
                stamp = datetime.fromisoformat(position[0])
            except ValueError:
                raise Refused("invalid ledger cursor") from None
            if stamp.tzinfo is not None:
                raise Refused("invalid ledger cursor")
            query = query.where(or_(OfLedgerEvidence.recorded_at < stamp,
                and_(OfLedgerEvidence.recorded_at == stamp, OfLedgerEvidence.evidence_hash > position[1])))
        rows = s.scalars(query.order_by(OfLedgerEvidence.recorded_at.desc(), OfLedgerEvidence.evidence_hash).limit(limit + 1)).all()
        page = rows[:limit]
        resolved = set(s.scalars(select(OfLedgerResolution.evidence_hash).where(
            OfLedgerResolution.account_id == account, OfLedgerResolution.binding_id == runtime.binding_id,
            OfLedgerResolution.evidence_hash.in_([row.evidence_hash for row in page]))))
        evidence = []
        for row in page:
            observed = json.loads(row.observation_json)
            evidence.append({"evidenceHash": row.evidence_hash, "providerTransactionId": row.provider_transaction_id,
                "fanId": str(row.fan_id) if row.fan_id is not None else None,
                "transactionId": str(row.transaction_id) if row.transaction_id is not None else None,
                "messageId": str(observed["message_id"]) if observed.get("message_id") is not None else None,
                "amountCents": observed.get("amount_cents"), "currency": observed.get("currency"),
                "status": observed.get("status"), "outcome": row.outcome,
                "resolved": row.evidence_hash in resolved, "recordedAt": z(row.recorded_at),
                "supportedAction": "reconcile_evidence" if row.evidence_hash not in resolved and observed.get("message_id") is not None else None,
                "blocker": "provider_message_identity_missing" if observed.get("message_id") is None else None})
        return {"ledgerVersion": "of-ledger.v1", "observedAt": z(utcnow()), **_ledger_core(s, runtime),
            "evidence": evidence, "nextCursor": encode("ledger-evidence", account, runtime.binding_id,
                [page[-1].recorded_at.isoformat(), page[-1].evidence_hash]) if len(rows) > limit else None}


def resolve_ledger(store, tenant, account, *, evidence_hash, command_key, actor, reason, expected_fingerprint):
    """Re-project only an exact provider purchase identity already in evidence.

    No amount/time matching, user-supplied fan/media IDs or ownership switches.
    Missing message identity and incompatible evidence remain unresolved.
    """
    command_identity(command_key, actor, reason, expected_fingerprint)
    if type(evidence_hash) is not str or re.fullmatch(r"[a-f0-9]{64}", evidence_hash) is None:
        raise Refused("invalid ledger evidence hash")
    request_hash = digest([evidence_hash, expected_fingerprint, actor, reason])
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        prior = _prior(s, runtime, command_key, "ledger_resolution", request_hash)
        if prior is not None:
            return prior
        before = _ledger_core(s, runtime)
        if before["fingerprint"] != expected_fingerprint:
            raise Refused("ledger state changed")
        evidence = s.get(OfLedgerEvidence, (account, evidence_hash))
        binding = s.get(OfCreatorBinding, runtime.binding_id)
        scan = s.get(OfLedgerScan, account)
        if (evidence is None or scan is None or scan.binding_id != runtime.binding_id
                or evidence.recorded_at < binding.created_at):
            raise Refused("ledger evidence unavailable")
        existing = s.get(OfLedgerResolution, (account, evidence_hash))
        if existing is not None:
            raise Refused("ledger evidence already resolved")
        observed = json.loads(evidence.observation_json)
        fan_id, mid = observed.get("fan_id"), observed.get("message_id")
        tx = s.scalar(select(Transaction).where(Transaction.account_id == account,
            Transaction.provider_transaction_id == evidence.provider_transaction_id).with_for_update())
        message = s.get(Message, (account, fan_id, mid), with_for_update=True) if fan_id is not None and mid is not None else None
        offers = s.scalars(select(ContentOffer).where(ContentOffer.account_id == account,
            ContentOffer.fan_id == fan_id, ContentOffer.offer_message_id == mid).with_for_update()).all() if mid is not None else []
        now, blocker = utcnow(), None
        from .ledger import PAID, TERMINAL, _status
        occurred = datetime.fromisoformat(observed["occurred_at"])
        if mid is None:
            blocker = "provider_message_identity_missing"
        elif observed.get("kind") != "ppv_message" or fan_id is None:
            blocker = "paid_message_identity_required"
        elif tx is not None and (tx.source != "ledger" or tx.fan_id != fan_id
                or tx.amount_cents != observed.get("amount_cents") or tx.currency != observed.get("currency")
                or tx.kind != observed.get("kind") or tx.message_id not in {None, mid}
                or json.loads(tx.raw_json or "{}").get("message_id") not in {None, mid}
                or tx.occurred_at.isoformat() != observed.get("occurred_at")):
            blocker = "transaction_identity_conflict"
        elif tx is not None and _status(tx.status, observed.get("status")) == "conflict":
            blocker = "transaction_status_conflict"
        elif (message is None or message.direction != "out" or len(offers) > 1
                or (not (tx is not None and tx.message_id == mid and observed.get("status") in TERMINAL)
                    and (message.is_unsent or message.price_cents != observed["amount_cents"]
                    or occurred < message.created_at
                    or offers and offers[0].price_cents != observed["amount_cents"]))):
            blocker = "canonical_paid_message_conflict"
        elif s.scalar(select(Transaction.id).where(Transaction.account_id == account,
                Transaction.fan_id == fan_id, Transaction.message_id == mid,
                Transaction.id != tx.id if tx is not None else Transaction.id.is_not(None)).limit(1)) is not None:
            blocker = "message_purchase_identity_conflict"
        financial_projection = None
        if blocker == "canonical_paid_message_conflict" and tx is None:
            # Older immutable conflicts may predate money/linkage separation.
            # This explicit audited replay recovers their verified money even
            # while history is unavailable; it never clears the linkage hold.
            from .ledger import _project_receipt
            projected, financial_outcome = _project_receipt(s, account, observed, now=now, backfill=True)
            if financial_outcome == "applied":
                financial_projection = projected.id
        result = {"recorded": True, "resolved": blocker is None, "evidenceHash": evidence_hash,
            "outcome": "verified_projection" if blocker is None else "unresolved", "blocker": blocker,
            "commandKey": command_key}
        if financial_projection is not None:
            result.update(financialRecorded=True, transactionId=financial_projection)
        _record(s, runtime, command_key, "ledger_resolution", request_hash, actor, reason, before, result, now)
        if blocker is None:
            from .commerce import settle_media_purchase
            was_paid = tx is not None and tx.status in PAID
            status = observed["status"]
            if tx is None:
                tx = Transaction(account_id=account, provider_transaction_id=evidence.provider_transaction_id,
                    fan_id=fan_id, kind="ppv_message", amount_cents=observed["amount_cents"],
                    currency=observed["currency"], status=status, occurred_at=occurred, source="ledger",
                    description=observed.get("description"), raw_json=canonical(observed),
                    cleared_at=datetime.fromisoformat(observed["cleared_at"]) if observed.get("cleared_at") else None,
                    **{key: observed.get(key) for key in ("net_cents", "fee_cents", "vat_cents", "payout_pending_days")})
                s.add(tx)
                s.flush()
            # A stale observation may link an existing paid transaction but can
            # never resurrect a terminal purchase or reverse a newer status.
            if _status(tx.status, status) != "stale":
                tx.status = status
            tx.message_id = mid
            if (tx.status in PAID) != was_paid:
                fan = s.get(Fan, (account, fan_id), with_for_update=True)
                fan.lifetime_spend_cents = max(0, fan.lifetime_spend_cents +
                    (tx.amount_cents if tx.status in PAID else -tx.amount_cents))
            settle_media_purchase(s, account_id=account, fan_id=fan_id, message=message,
                offer=offers[0] if offers else None, paid=tx.status in PAID, was_paid=was_paid,
                stamp=tx.occurred_at, now=now)
            s.add(OfLedgerResolution(account_id=account, evidence_hash=evidence_hash,
                binding_id=runtime.binding_id, command_key=command_key, transaction_id=tx.id,
                fan_id=fan_id, message_id=mid, decision_json=canonical({"observed": observed,
                    "transactionStatus": tx.status, "actor": actor, "reason": reason}), created_at=now))
            if occurred >= now - timedelta(minutes=5):
                from .launch_engagement.service import invalidate
                invalidate(s, runtime, reason="purchase" if tx.status in PAID else "refund", actor=actor,
                    fan_id=fan_id, now=now)
        return result


def exclude_unresolved_ledger(store, tenant, account, *, transaction_id, command_key, actor, reason, expected_fingerprint):
    """Stop one unlinked paid purchase from holding the whole account; it keeps holding its own fan.

    Only a purchase whose every provider observation lacks a message identity may be excluded. Nothing is
    linked, guessed, cleared or created: the Transaction is untouched and stays unresolved for its fan.
    """
    command_identity(command_key, actor, reason, expected_fingerprint)
    if type(transaction_id) is not int or not 0 < transaction_id < 2**31:
        raise Refused("invalid ledger transaction")
    request_hash = digest([transaction_id, expected_fingerprint, actor, reason])
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        prior = _prior(s, runtime, command_key, "ledger_exclusion", request_hash)
        if prior is not None:
            return prior
        before = _ledger_core(s, runtime)
        if before["fingerprint"] != expected_fingerprint:
            raise Refused("ledger state changed")
        binding = s.get(OfCreatorBinding, runtime.binding_id)
        tx = s.scalar(select(Transaction).where(Transaction.id == transaction_id,
            Transaction.account_id == account).with_for_update())
        if tx is None:
            raise Refused("ledger transaction unavailable")
        from .ledger import PAID
        if (tx.source != "ledger" or tx.kind != "ppv_message" or tx.message_id is not None or tx.fan_id is None
                or tx.status not in PAID or tx.occurred_at < binding.created_at):
            raise Refused("transaction is not an unresolved paid purchase of this binding")
        if s.scalar(select(OfLedgerExclusion.id).where(OfLedgerExclusion.account_id == account,
                OfLedgerExclusion.transaction_id == tx.id)) is not None:
            raise Refused("ledger transaction already excluded")
        evidence = s.scalars(select(OfLedgerEvidence).where(OfLedgerEvidence.account_id == account,
            OfLedgerEvidence.provider_transaction_id == tx.provider_transaction_id)
            .order_by(OfLedgerEvidence.evidence_hash)).all()
        # The same exact test resolve_ledger applies: a retained message identity anywhere means this is not
        # an identity-missing case and must go through resolution instead.
        if (tx.provider_transaction_id is None or not evidence
                or any(json.loads(row.observation_json).get("message_id") is not None for row in evidence)
                or json.loads(tx.raw_json or "{}").get("message_id") is not None):
            raise Refused("transaction blocker is not provider_message_identity_missing")
        blocker, now = "provider_message_identity_missing", utcnow()
        evidence_hash = digest([account, runtime.binding_id, tx.id, tx.provider_transaction_id,
            [row.evidence_hash for row in evidence]])
        result = {"recorded": True, "excluded": True, "transactionId": tx.id, "fanId": tx.fan_id,
            "blocker": blocker, "evidenceHash": evidence_hash, "commandKey": command_key}
        _record(s, runtime, command_key, "ledger_exclusion", request_hash, actor, reason, before, result, now)
        s.add(OfLedgerExclusion(account_id=account, binding_id=runtime.binding_id, fan_id=tx.fan_id,
            transaction_id=tx.id, blocker=blocker, reason=reason, actor=actor, command_key=command_key,
            evidence_hash=evidence_hash, created_at=now))
        s.flush()
        return result


def _readiness(s, runtime, *, credential_reader=None):
    """Truthful readiness from current owners, with explicit capability limits."""
    from db.models import AccountAiConfig, CatalogItem, GrokDailyCost, OfConversationState
    from db.schema import HEAD, check_schema
    from .configuration import fingerprint
    from .launch_engagement.models import Settings
    from .launch_engagement.contracts import Policy, load_object
    from .launch_history_memory.memory import _receipt_matches
    from .launch_history_memory.models import MemoryReceipt
    from .launch_history_memory.recovery import OfHistoryCursor, OfHistoryScan
    from .launch_personality.models import PersonalityRevision
    from .launch_personality.policy import readiness as ai_readiness
    from .media_delivery import catalog_terms
    now, account = utcnow(), runtime.account_id
    checks = []
    def check(key, status, blocker, action, *, blocking=True, observed=None):
        checks.append({"key": key, "status": status, "blocking": blocking,
            "observedAt": z(observed or now), "blocker": blocker, "action": action})
    check_schema(s.connection())
    check("schema", "ready", None, None)
    connection = s.get(OfProviderConnection, account)
    connected = bool(connection and connection.binding_id == runtime.binding_id and connection.status == "verified"
        and connection.verified_at and connection.expires_at > now and connection.last_error is None)
    check("connection", "ready" if connected else "blocked", None if connected else "connection_not_current",
        None if connected else "connection", observed=connection.updated_at if connection else None)
    health = s.get(OfLaunchWorkerHealth, account)
    internal_capabilities = load_object(health.capabilities_json) if health and health.binding_id == runtime.binding_id else {}
    worker_current = bool(connected and health and health.binding_id == runtime.binding_id
        and type(internal_capabilities.get("connection_revision")) is int
        and internal_capabilities["connection_revision"] == connection.revision
        and timedelta(0) <= now - health.observed_at <= timedelta(minutes=2) and health.last_error is None)
    check("worker", "ready" if worker_current else "unknown", None if worker_current else "worker_observation_missing_or_stale",
        None if worker_current else "worker_status", observed=health.observed_at if health else None)
    # The connection fence is internal metadata, not a new dashboard capability.
    capabilities = {key: internal_capabilities.get(key) is True
        for key in ("history_recovery", "history_memory", "engagement")}
    recent = runtime.last_sync_at is not None and timedelta(0) <= now - runtime.last_sync_at <= timedelta(minutes=15)
    check("recentIntake", "ready" if recent and not runtime.recovery_needed else "running",
        None if recent and not runtime.recovery_needed else "recent_recovery_pending", "history_backfill",
        observed=runtime.last_sync_at)
    scan = s.get(OfHistoryScan, account)
    pending_archive = s.scalar(select(func.count()).select_from(OfHistoryCursor).where(
        OfHistoryCursor.account_id == account, OfHistoryCursor.binding_id == runtime.binding_id,
        OfHistoryCursor.status != "complete"))
    archive_complete = bool(scan and scan.binding_id == runtime.binding_id and scan.status == "complete"
        and scan.known_fans_complete and not pending_archive)
    archive_running = worker_current and capabilities.get("history_recovery") is True
    check("archive", "ready" if archive_complete else "running" if archive_running else "disabled",
        None if archive_complete else "available_history_recovery_incomplete" if archive_running else "history_recovery_worker_disabled_or_unobserved",
        "history_backfill" if archive_running else "worker_configuration", blocking=False,
        observed=scan.updated_at if scan else None)
    stored = s.scalar(select(func.count()).select_from(Message).where(Message.account_id == account))
    processed = s.scalar(select(func.count()).select_from(Message).join(MemoryReceipt, _receipt_matches())
        .where(Message.account_id == account))
    memory_running = worker_current and capabilities.get("history_memory") is True
    check("memory", "ready" if stored == processed else "running" if memory_running else "disabled",
        None if stored == processed else "stored_memory_processing_incomplete" if memory_running else "history_memory_worker_disabled_or_unobserved",
        "history_memory" if memory_running else "worker_configuration", blocking=False)
    ledger = _ledger_core(s, runtime)
    ledger_ready = ledger["fresh"] and not ledger["commerceHold"]
    check("ledger", "ready" if ledger_ready else "blocked", None if ledger_ready else "ledger_held_or_stale", "ledger")
    config = s.get(AccountAiConfig, account)
    credentials = credential_reader(s, account, runtime.binding_id) if credential_reader else None
    ai = ai_readiness({c.name: getattr(config, c.name) for c in config.__table__.columns} if config else {}, credentials)
    for name in ("model", "credential", "budget"):
        state = ai[name]["state"]
        if name == "budget" and state == "ready":
            spent = s.scalar(select(func.coalesce(func.sum(GrokDailyCost.cost_cents), 0)).where(
                GrokDailyCost.account_id == account, GrokDailyCost.day == now.date().isoformat(),
                GrokDailyCost.provider == ai["model"].get("provider")))
            # The existing GrokDailyCost owner stores hundredths of a cent in
            # its historically named cost_cents column (native budget units).
            if spent >= ai["budget"]["dailyCostCapCents"] * 100:
                state = "exhausted"
        check(name, "ready" if state == "ready" else "blocked", None if state == "ready" else name + "_" + state,
            "ai_configuration" if name != "credential" else "connection_credentials")
    # A new review draft does not revoke the prior saved configuration. Match
    # current configuration evidence independently of the latest draft head,
    # while refusing approval from another creator binding or older settings.
    approval = s.scalar(select(PersonalityRevision).where(
        PersonalityRevision.account_id == account,
        PersonalityRevision.binding_id == runtime.binding_id,
        PersonalityRevision.state == "approved",
        PersonalityRevision.configuration_revision == runtime.configuration_revision,
        PersonalityRevision.configuration_fingerprint == fingerprint(config))
        .order_by(PersonalityRevision.revision.desc()).limit(1)) if config else None
    approved = approval is not None
    facts_blocker = None
    if config is not None:
        from .creator_facts import effective
        try:
            _, facts_source = effective(s, config, now=now)
            if facts_source is not None and facts_source["state"] != "current":
                facts_blocker = "creator_facts_" + facts_source["state"]
        except Refused:
            facts_blocker = "creator_facts_binding_mismatch"
    approved = approved and facts_blocker is None
    check("personality", "ready" if approved else "blocked",
        None if approved else facts_blocker or "approved_current_personality_required", "personality")
    policy = s.get(OfDeliveryPolicy, account)
    delivery = bool(policy and policy.enabled and policy.transport_kind == "native" and policy.limit_units > policy.used_units)
    check("deliveryPolicy", "ready" if delivery else "blocked", None if delivery else "native_delivery_policy_unavailable", "delivery_policy")
    items = s.scalars(select(CatalogItem).where(CatalogItem.account_id == account, CatalogItem.enabled.is_(True),
        CatalogItem.pricing_mode == "fixed").order_by(CatalogItem.id).limit(201)).all()
    usable = 0
    for item in items[:200]:
        try:
            catalog_terms(s, account, item.id)
        except Refused:
            continue
        usable += 1
    check("catalogue", "ready" if usable else "blocked", None if usable else "approved_usable_fixed_catalogue_required", "catalogue")
    settings = s.get(Settings, account)
    engagement = Policy.parse(load_object(settings.settings_json)) if settings and settings.binding_id == runtime.binding_id else Policy()
    check("engagement", "ready" if engagement.enabled and capabilities.get("engagement") is True and worker_current else "disabled",
        None if engagement.enabled and capabilities.get("engagement") is True and worker_current else "engagement_settings_or_worker_disabled",
        "engagement", blocking=False)
    automated = s.scalar(select(func.count()).select_from(OfConversationState).where(
        OfConversationState.account_id == account, OfConversationState.mode == "automated",
        OfConversationState.takeover_state == "none"))
    return {"readinessVersion": "of-launch-readiness.v1", "observedAt": z(now), "schemaRevision": HEAD,
        "readyToEnable": not any(c["blocking"] and c["status"] != "ready" for c in checks),
        "checks": checks, "accountControl": account_state(s, runtime),
        "coverage": {"storedMessages": stored, "processedMessages": processed, "providerTotalMessages": None,
            "archiveComplete": archive_complete, "pendingArchiveConversations": pending_archive},
        "catalogue": {"usableInspectedItems": usable, "inspectedItems": min(len(items), 200), "hasMore": len(items) > 200},
        "autonomy": {"accountEnabled": runtime.enabled, "deliveryEnabled": bool(policy and policy.enabled),
            "existingAutomatedConversations": automated,
            "automateNewConversations": False,   # locked off: new fans are never auto-enabled (store.NEW_CONVERSATION_AUTOMATION_LOCKED)
            "engagementEnabled": engagement.enabled, "workerCapabilities": capabilities},
        "limits": ["Conservative English-pattern historical facts and lexical source retrieval.",
            "Provider history totals are unknown; stored processing is a separate measure."]}


def launch_readiness(store, tenant, account, *, credential_reader=None):
    with Session(store.engine) as s, s.begin():
        runtime = read_scope(store, s, tenant, account)
        return _readiness(s, runtime, credential_reader=credential_reader)
