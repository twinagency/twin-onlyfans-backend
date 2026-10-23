"""Continuous paid-offer import: schedule, honest coverage, echoes and source conflicts.

Everything here is database work; the only provider reads it causes are the
existing bounded refresh job and vault sync job it enqueues (one extra page per
account per five seconds stays in their workers). It never sends a message,
publishes a post, changes outgoing authority/chat modes, or calls a provider
delete. A scheduled pass is leased per account and every commit revalidates
the lease, binding, connection revision and enabled flag, so a rebound,
reconnected, disabled or stolen scope cannot write stale results.
"""
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
import os
import re
import uuid

from sqlalchemy import and_, exists, func, or_, select, true
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from onlyfans_read.agency_scope import actor_allowed
from onlyfans_read.contract import ID_PATTERN
from db.models import CatalogItem, CATALOG_IS_SINGLE, Message, OfCreatorBinding, VaultItem
from db.onlyfans_models import OfRuntime
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_content_models import OfContentChange
from db.onlyfans_delivery_models import OfTextIntent
from db.onlyfans_vault_models import OfVaultObservation, OfVaultSyncJob
from db.onlyfans_offer_import_models import (OfContentSyncAccount as Account, OfContentSyncConflict as Conflict,
    OfContentSyncDecision as Decision, OfContentSyncDraft as Draft, OfContentSyncOperation as Operation,
    OfOfferImportCandidate, OfOfferImportCursor, OfOfferImportDecision, OfOfferImportJob, OfOfferObservation)
from .content_management import digest, fingerprint as item_fingerprint, _revision
from .native_context import canonical
from .offer_terms import MAX_ATTACHMENTS, MAX_PRICE_CENTS, MIN_PRICE_CENTS, media_key
from .store import Refused, utcnow

INCREMENTAL_SECONDS = 300
JITTER_SECONDS = 30
DELAYED_SECONDS = 2 * INCREMENTAL_SECONDS
RECONCILE_SECONDS = 24 * 3600
DRAIN_SECONDS = 5
LEASE_SECONDS = 60
FOLD_ROWS = 1000
FOLD_BATCHES = 3
BACKLOG_PROBE = 10001
REFRESH_SOURCES = 50
VAULT_MIN_INTERVAL_SECONDS = 240
PRIORITY_QUOTA = 100
ROTATION_QUOTA = 100
BACKOFF_CAP_SECONDS = 3600
SYNC_KINDS = ("incremental", "reconcile")
PASS_KINDS = ("incremental", "reconcile", "sync_now")
PAID_POSTS_DETAIL = "Paid-post reading is not verified yet."
RETRYABLE_ERRORS = {"pass_failed", "connection_unavailable"}


class InvalidSync(ValueError):
    pass


class StaleClaim(Exception):
    """The leased scope changed (binding, connection revision, lease, enabled)."""


def _date(value):
    return value.isoformat() + "Z" if value else None


def _identity(command_key, actor):
    if (type(command_key) is not str or not re.fullmatch(r"[a-f0-9]{64}", command_key)
            or not actor_allowed(actor)):
        raise InvalidSync("The sync command identity is invalid.")


def _uuid(value):
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value):
        raise InvalidSync("That sync reference is invalid.")
    return value


def _ids(raw):
    try:
        value = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return None
    if type(value) is not list or any(type(v) is not int or not 0 < v < 2**63 for v in value) or len(set(value)) != len(value):
        return None
    return value


def jitter(account, at):
    """Deterministic spread in [-30, +30] s, so accounts do not pulse together."""
    h = int(hashlib.sha256(f"{account}|{at.isoformat()}".encode()).hexdigest()[:8], 16)
    return h % (2 * JITTER_SECONDS + 1) - JITTER_SECONDS


def _next_incremental(account, now):
    return now + timedelta(seconds=INCREMENTAL_SECONDS + jitter(account, now))


def _worker_guard(tenant, limit):
    if os.environ.get("ONLYFANS_PROCESS_ROLE") != "worker":
        raise Refused("content sync requires the worker role")
    if type(tenant) is not str or tenant == "deployment" or not ID_PATTERN.fullmatch(tenant):
        raise Refused("invalid content sync tenant")
    if type(limit) is not int or not 1 <= limit <= 4:
        raise Refused("invalid content sync work bound")


def _auth_ok(runtime, connection, now):
    return (connection is not None and connection.binding_id == runtime.binding_id
        and connection.status == "verified" and connection.verified_at is not None and connection.expires_at > now)


def _connection(s, runtime):
    from .vault_management import _connection as verified
    return verified(s, runtime)


# ── Identities: echoes and holds ────────────────────────────────────────

def echo_map(s, account, binding_id, source_ids):
    """{provider message id: catalogue item} for messages this CRM itself sent."""
    ids = [i for i in set(source_ids) if type(i) is int]
    if not ids:
        return {}
    return dict(s.execute(select(OfTextIntent.provider_message_id, OfTextIntent.catalog_item_id).where(
        OfTextIntent.account_id == account, OfTextIntent.binding_id == binding_id,
        OfTextIntent.catalog_item_id.is_not(None), OfTextIntent.provider_message_id.in_(ids))).all())


def echo_item(session, account, binding_id, source_id):
    """The catalogue item whose outgoing paid message this observed source is, if any."""
    return echo_map(session, account, binding_id, [source_id]).get(source_id)


def selected_observation(s, account, binding_id, item_id):
    """The authoritative source of an import-created item: create decision, then accepted changes."""
    create = s.scalar(select(OfOfferImportDecision).where(OfOfferImportDecision.account_id == account,
        OfOfferImportDecision.binding_id == binding_id, OfOfferImportDecision.item_id == item_id,
        OfOfferImportDecision.action == "create"))
    if create is None:
        return None
    withdrawn = s.scalar(select(OfOfferImportDecision.command_key).where(OfOfferImportDecision.account_id == account,
        OfOfferImportDecision.item_id == item_id, OfOfferImportDecision.action == "withdraw")) is not None
    if withdrawn:
        return None
    accepted = s.scalar(select(Conflict.observation_id).where(Conflict.account_id == account, Conflict.item_id == item_id,
        Conflict.status == "resolved", Conflict.resolution == "accept_source").order_by(Conflict.resolved_at.desc()).limit(1))
    return s.get(OfOfferObservation, (account, accepted or create.observation_id))


def _changed_sources(s, account, binding_id, *, item_id=None, limit=1000):
    """(item id, selected observation, new observation) for unrecorded source changes.

    A change is a NEW observation (never seen before, so observed after the
    selected one) of the same source id with a different fingerprint. A different
    source id carrying the same media is a price variant and never matches.
    """
    query = select(OfOfferImportDecision.item_id).where(OfOfferImportDecision.account_id == account,
        OfOfferImportDecision.binding_id == binding_id, OfOfferImportDecision.action == "create")
    if item_id is not None:
        query = query.where(OfOfferImportDecision.item_id == item_id)
    found = []
    for (item,) in s.execute(query.order_by(OfOfferImportDecision.item_id).limit(limit)).all():
        selected = selected_observation(s, account, binding_id, item)
        if selected is None:
            continue
        for new in s.scalars(select(OfOfferObservation).where(OfOfferObservation.account_id == account,
                OfOfferObservation.binding_id == binding_id, OfOfferObservation.source_kind == selected.source_kind,
                OfOfferObservation.source_id == selected.source_id, OfOfferObservation.fingerprint != selected.fingerprint,
                OfOfferObservation.observed_at > selected.observed_at,
                ~exists().where(Conflict.account_id == account, Conflict.item_id == item,
                    Conflict.observation_id == OfOfferObservation.observation_id))
                .order_by(OfOfferObservation.observed_at, OfOfferObservation.observation_id).limit(5)):
            if new.source_kind == "message" and echo_item(s, account, binding_id, new.source_id) is not None:
                continue
            found.append((item, selected, new))
    return found


def detect_conflicts(s, account, binding_id, now):
    """Open one conflict per newly observed change to a selected source; returns the count."""
    opened = 0
    for item, selected, new in _changed_sources(s, account, binding_id):
        s.add(Conflict(account_id=account, conflict_id=str(uuid.uuid4()), binding_id=binding_id, item_id=item,
            kind="source_changed", prior_fingerprint=selected.fingerprint, observation_id=new.observation_id,
            status="open", revision=1, created_at=now, updated_at=now))
        opened += 1
    if opened:
        s.flush()
    return opened


def open_conflict(session, account, item_id):
    """Whether new sales of this item are held for an unresolved source change.

    Uses recorded conflicts and, once automatic sync is configured, a changed
    selected-source observation the scheduler has not recorded yet.
    """
    if session.scalar(select(Conflict.conflict_id).where(Conflict.account_id == account, Conflict.item_id == item_id,
            Conflict.status == "open").limit(1)) is not None:
        return True
    sync = session.get(Account, account)
    if sync is None or not sync.enabled:
        return False
    return bool(_changed_sources(session, account, sync.binding_id, item_id=item_id, limit=1))


def _media_states(s, account, runtime, connection, media):
    """Per-media readiness at the current verified connection: 'ok' | 'unusable' | 'unverified'."""
    from .vault_management import _ready
    states = {}
    for mid in media:
        row = s.get(VaultItem, (account, mid))
        if row is None:
            states[mid] = "unverified"
            continue
        observation = s.get(OfVaultObservation, (account, mid))
        job = s.get(OfVaultSyncJob, (account, observation.job_id)) if observation else None
        ok, why = _ready(row, observation, job, connection, runtime.binding_id)
        # Only an authoritative removal is unusable; stale ownership, processing
        # or unknown readiness (expired session, incomplete page) is not deletion.
        states[mid] = "ok" if ok else "unusable" if why == "removed" else "unverified"
    return states


def media_unusable(session, account, item):
    """Verified unusable required media for an item (False when sync was never configured)."""
    if session.get(Account, account) is None:
        return False
    ids = _ids(item.media_ids)
    runtime = session.get(OfRuntime, account)
    if not ids or runtime is None:
        return False
    connection = session.get(OfProviderConnection, account)
    return "unusable" in _media_states(session, account, runtime, connection, ids).values()


def sale_hold(session, account, item):
    """Reason a catalogue item may not be sold now, or None."""
    if open_conflict(session, account, item.id):
        return "open_conflict"
    if media_unusable(session, account, item):
        return "media_unusable"
    return None


def source_state(s, account, observation):
    """'observed' | 'unavailable' | 'unverified' for the item's selected source message."""
    if observation is None or observation.source_kind != "message" or observation.fan_id is None:
        return "unverified"
    message = s.get(Message, (account, observation.fan_id, observation.source_id))
    if message is not None and message.is_unsent:
        return "unavailable"
    for cursor in s.scalars(select(OfOfferImportCursor).where(OfOfferImportCursor.account_id == account,
            OfOfferImportCursor.fan_id == observation.fan_id).order_by(OfOfferImportCursor.updated_at.desc()).limit(20)):
        try:
            targets, found = json.loads(cursor.targets_json), json.loads(cursor.found_json)
        except ValueError:
            continue
        if observation.source_id in targets:
            if observation.source_id in found:
                return "observed"
            if cursor.status == "unresolved" and cursor.outcome == "source_not_found":
                # The history walked past it without seeing it: deleted/unsent.
                # A spent budget or provider error stays "unverified".
                return "unavailable"
    return "observed" if message is not None else "unverified"


def item_health(session, account, item_id):
    """Why an item can or cannot be sold plus what is known about its source and media."""
    item = session.scalar(select(CatalogItem).where(CatalogItem.account_id == account, CatalogItem.id == item_id))
    if item is None:
        raise Refused("sync record unavailable")
    runtime = session.get(OfRuntime, account)
    connection = session.get(OfProviderConnection, account)
    ids = _ids(item.media_ids) or []
    states = _media_states(session, account, runtime, connection, ids)
    selected = selected_observation(session, account, runtime.binding_id, item_id)
    return {"itemId": str(item_id), "salesHold": sale_hold(session, account, item),
        "sourceState": source_state(session, account, selected) if selected else "none",
        "unusableMedia": [str(m) for m, v in states.items() if v == "unusable"],
        "unverifiedMedia": [str(m) for m, v in states.items() if v == "unverified"]}


# ── Status ──────────────────────────────────────────────────────────────

def upload_counts(session, account, binding_id):
    keys = {"draft": "draft", "queued": "queued", "uploading": "uploading", "verifying": "verifying",
        "synced": "synced", "retrying": "retrying", "connection_required": "connectionRequired",
        "needs_review": "needsReview", "outcome_unconfirmed": "outcomeUnconfirmed"}
    counts = {value: 0 for value in keys.values()}
    for state, count in session.execute(select(Draft.state, func.count()).where(Draft.account_id == account,
            Draft.binding_id == binding_id).group_by(Draft.state)).all():
        if state in keys:
            counts[keys[state]] = count
    return counts


def _coverage_state(now, checked, *, partial=False, started=True):
    if not started or checked is None:
        return "not_started"
    if (now - checked).total_seconds() > DELAYED_SECONDS:
        return "delayed"
    return "partial" if partial else "current"


def _vault_coverage(s, account, enabled, now):
    if not enabled:
        return {"state": "not_started", "checkedAt": None, "detail": "Automatic sync is off."}
    jobs = s.scalars(select(OfVaultSyncJob).where(OfVaultSyncJob.account_id == account)
        .order_by(OfVaultSyncJob.requested_at.desc()).limit(10)).all()
    done = next((j for j in jobs if j.status == "complete"), None)
    latest = jobs[0] if jobs else None
    if done is None:
        state = "partial" if latest is not None and latest.status in {"pending", "running"} else "not_started"
        return {"state": state, "checkedAt": None,
            "detail": "Reading the vault." if state == "partial" else "The vault has not been read yet."}
    partial = latest.status == "failed"
    state = _coverage_state(now, done.completed_at, partial=partial)
    detail = ("The latest vault read failed; showing the last complete read." if partial
        else "The last complete vault read is more than ten minutes old." if state == "delayed"
        else "Vault media is up to date.")
    return {"state": state, "checkedAt": _date(done.completed_at), "detail": detail}


def _message_coverage(row, job, enabled, now, paused):
    if not enabled:
        return {"state": "not_started", "checkedAt": None, "detail": "Automatic sync is off."}
    initial_done = job is not None and job.status == "complete"
    if row.last_incremental_at is None:
        scanning = job is not None and job.status in {"pending", "running"}
        return {"state": "partial" if scanning else "not_started", "checkedAt": None,
            "detail": "Scanning imported history." if scanning else "The first check has not run yet."}
    partial = row.backlog > 0 or not initial_done
    state = _coverage_state(now, row.last_incremental_at, partial=partial)
    if state == "delayed":
        detail = "The account connection is required to continue." if paused else "The last successful check is more than ten minutes old."
    elif row.backlog > 0:
        detail = f"{row.backlog} newer paid messages are waiting to be checked."
    elif not initial_done:
        detail = "Older imported history is still being scanned." if job is not None and job.status in {"pending", "running"} \
            else "The initial history scan stopped at its limit."
    else:
        detail = "Paid messages already imported are up to date."
    return {"state": state, "checkedAt": _date(row.last_incremental_at), "detail": detail}


def _sync_view(s, runtime, row, now):
    account = runtime.account_id
    connection = s.get(OfProviderConnection, account)
    ok = _auth_ok(runtime, connection, now)
    enabled = row is not None and row.enabled
    if row is None or (not row.enabled and row.status != "blocked"):
        status, reason = "off", None
    elif row.status == "blocked":
        status, reason = "blocked", row.blocked_reason
    elif row.binding_id != runtime.binding_id:
        status, reason = "blocked", "binding_changed"
    elif not ok:
        status, reason = "paused_auth", "connection_required"
    else:
        status, reason = "active", None
    job = s.get(OfOfferImportJob, (account, row.initial_job_id)) if row is not None and row.initial_job_id else None
    openers = s.scalar(select(func.count()).select_from(Conflict).where(Conflict.account_id == account, Conflict.status == "open")) or 0
    zero = row is None
    return {"enabled": enabled, "status": status, "revision": 0 if zero else row.revision, "blockedReason": reason,
        "lastIncrementalAt": None if zero else _date(row.last_incremental_at),
        "nextIncrementalAt": _date(row.incremental_due_at) if enabled else None,
        "lastReconcileAt": None if zero else _date(row.last_reconcile_at),
        "nextReconcileAt": _date(row.reconcile_due_at) if enabled else None,
        "lastFullReconcileAt": None if zero else _date(row.last_full_reconcile_at),
        "coveredThroughMessageId": None if zero or row.covered_through_message_id is None else str(row.covered_through_message_id),
        "backlog": 0 if zero else row.backlog,
        "initial": {"jobId": job.job_id if job else None, "status": job.status if job else "not_started",
            "rowsScanned": job.rows_scanned if job else 0, "complete": job is not None and job.status == "complete"},
        "coverage": {
            "media": _vault_coverage(s, account, enabled, now),
            "paidMessages": _message_coverage(row, job, enabled, now, status == "paused_auth") if row is not None else
                {"state": "not_started", "checkedAt": None, "detail": "Automatic sync is off."},
            # Reading paid feed posts needs an authenticated, verified contract first.
            "paidPosts": {"state": "unsupported", "checkedAt": None, "detail": PAID_POSTS_DETAIL}},
        "openConflicts": openers,
        "uploads": upload_counts(s, account, runtime.binding_id)}


def get_status(store, tenant, account, *, now=None):
    now = now or utcnow()
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        return {"accountId": account, "sync": _sync_view(s, runtime, s.get(Account, account), now)}


# ── Settings ────────────────────────────────────────────────────────────

def _key_taken(s, account, key, table):
    """One command key identifies one command, whichever table recorded it."""
    for model in (Decision, Operation, Draft):
        if model is not table and s.scalar(select(model.command_key).where(model.account_id == account,
                model.command_key == key)) is not None:
            raise Refused("sync idempotency conflict")


def _ensure_initial(s, runtime, row, now, seed):
    """The inventory job the incremental fold feeds; reused when it is still valid for this binding."""
    from .offer_import import create_inventory_job
    account = runtime.account_id
    job = s.get(OfOfferImportJob, (account, row.initial_job_id)) if row.initial_job_id else None
    if (job is not None and job.binding_id == runtime.binding_id
            and job.status in {"pending", "running", "complete", "partial"}):
        if row.covered_through_message_id is None:
            row.covered_through_message_id = job.upper_message_id
        return job
    job = s.scalar(select(OfOfferImportJob).where(OfOfferImportJob.account_id == account,
        OfOfferImportJob.mode == "inventory", OfOfferImportJob.status.in_(("pending", "running"))).limit(1).with_for_update())
    if job is not None and job.binding_id != runtime.binding_id:
        # A scan of the previous binding is never inherited.
        from .offer_import import _terminal
        _terminal(job, "superseded", "binding_changed")
        s.flush()
        job = None
    if job is None:
        key = digest(["content-sync-initial", account, runtime.binding_id, seed])
        job = create_inventory_job(s, runtime, command_key=key, request_hash=digest({"mode": "inventory", "sources": []}),
            actor=row.actor, now=now)
    row.initial_job_id, row.covered_through_message_id = job.job_id, job.upper_message_id
    s.add(Operation(account_id=account, operation_id=str(uuid.uuid4()), binding_id=runtime.binding_id,
        command_key=digest(["initial-operation", account, job.job_id]), request_hash=digest({"initial": job.job_id}),
        actor=row.actor, kind="initial", status="queued", connection_revision=row.connection_revision,
        coverage_json=canonical({"source": "imported_history"}), result_json=canonical({"jobId": job.job_id}),
        attempts=0, created_at=now, updated_at=now))
    return job


def _cancel_active(s, account, now, reason):
    for op in s.scalars(select(Operation).where(Operation.account_id == account, Operation.kind.in_(PASS_KINDS),
            Operation.status.in_(("queued", "running"))).with_for_update()):
        op.status, op.last_error, op.lease_token, op.lease_until = "cancelled", reason, None, None
        op.updated_at = op.completed_at = now


def _park(s, row, runtime, now, reason):
    """The scope was rebound or lost: old cursors and mappings are never reused."""
    _cancel_active(s, row.account_id, now, reason)
    row.enabled, row.status, row.blocked_reason = False, "blocked", reason
    row.initial_job_id = row.covered_through_message_id = row.reconcile_after_item_id = None
    row.reconcile_cycle_started_at = row.last_incremental_at = row.last_reconcile_at = row.last_full_reconcile_at = None
    row.incremental_due_at = row.reconcile_due_at = None
    row.lease_token = row.lease_until = None
    row.backlog, row.updated_at = 0, now


def _state(row):
    return {"enabled": False, "revision": 0} if row is None else {"enabled": row.enabled, "revision": row.revision,
        "status": row.status, "initialJobId": row.initial_job_id}


def set_settings(store, tenant, account, *, enabled, expected_revision, command_key, actor):
    """Versioned on/off. Enabling starts or reuses this account's initial inventory only."""
    _identity(command_key, actor)
    if type(enabled) is not bool or type(expected_revision) is not int or not 0 <= expected_revision < 2**31 - 1:
        raise InvalidSync("Send a boolean enabled flag and the current settings revision.")
    request_hash = digest({"action": "settings", "enabled": enabled, "expectedRevision": expected_revision})
    now = utcnow()
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        prior = s.get(Decision, (account, command_key))
        if prior:
            if (prior.action, prior.request_hash, prior.actor, prior.binding_id) != ("settings", request_hash, actor, runtime.binding_id):
                raise Refused("sync idempotency conflict")
            return json.loads(prior.result_json)
        _key_taken(s, account, command_key, Decision)
        row = s.get(Account, account, with_for_update=True)
        revision = 0 if row is None else row.revision
        if revision != expected_revision:
            raise Refused("sync revision conflict")
        before = _state(row)
        if enabled:
            connection = _connection(s, runtime)
            if row is None:
                row = Account(account_id=account, binding_id=runtime.binding_id, enabled=True, status="active",
                    revision=1, actor=actor, command_key=command_key, backlog=0, failures=0, created_at=now, updated_at=now)
                s.add(row)
            elif row.binding_id != runtime.binding_id:
                # Rebinding: nothing mapped under the old binding carries over.
                _park(s, row, runtime, now, "binding_changed")
                row.binding_id = runtime.binding_id
            row.enabled, row.status, row.blocked_reason = True, "active", None
            row.revision, row.actor, row.command_key = revision + 1, actor, command_key
            row.connection_revision, row.failures, row.updated_at = connection.revision, 0, now
            row.incremental_due_at = now
            if row.reconcile_due_at is None:
                row.reconcile_due_at = now + timedelta(seconds=RECONCILE_SECONDS)
            s.flush()
            _ensure_initial(s, runtime, row, now, command_key)
        else:
            if row is None:
                row = Account(account_id=account, binding_id=runtime.binding_id, enabled=False, status="off",
                    revision=1, actor=actor, command_key=command_key, backlog=0, failures=0, created_at=now, updated_at=now)
                s.add(row)
            else:
                _cancel_active(s, account, now, "sync_off")
                row.enabled, row.status, row.blocked_reason = False, "off", None
                row.revision, row.actor, row.command_key = revision + 1, actor, command_key
                row.lease_token = row.lease_until = None
                row.incremental_due_at = row.reconcile_due_at = None
                row.updated_at = now
        s.flush()
        result = {"sync": _sync_view(s, runtime, row, now)}
        s.add(Decision(account_id=account, command_key=command_key, binding_id=runtime.binding_id, action="settings",
            request_hash=request_hash, actor=actor, subject_id=None, before_json=canonical(before),
            after_json=canonical(_state(row)), result_json=canonical(result), created_at=now))
        return result


# ── Scheduler ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Claim:
    account: str
    binding_id: str
    operation_id: str
    token: str
    kind: str
    sync_kind: str
    connection_revision: int
    now: object


def _scheduled_key(kind, account, binding, due):
    return digest(["schedule", kind, account, binding, due.isoformat() if due else None, str(uuid.uuid4())[:8]])


def claim_due(store, tenant, *, now=None, skip=()):
    """Lease one due account (or None). Parks rebound scopes and pauses lost auth.

    Order is most-overdue first, and each pass pushes its own due time forward,
    so a busy account cannot starve the others.
    """
    now = now or utcnow()
    skip = set(skip)
    with Session(store.engine) as s, s.begin():
        queued = exists().where(Operation.account_id == Account.account_id, Operation.kind == "sync_now",
            or_(Operation.status == "queued", and_(Operation.status == "running", Operation.lease_until <= now)))
        picked = s.execute(select(Account.account_id).join(OfRuntime, OfRuntime.account_id == Account.account_id)
            .join(OfCreatorBinding, OfCreatorBinding.id == OfRuntime.binding_id)
            .where(OfCreatorBinding.tenant_id == tenant, OfCreatorBinding.revoked_at.is_(None),
                OfCreatorBinding.channel == "onlyfans", Account.enabled.is_(True), Account.status.in_(("active", "paused_auth")),
                or_(Account.lease_token.is_(None), Account.lease_until <= now),
                or_(Account.incremental_due_at <= now, Account.reconcile_due_at <= now, queued),
                Account.account_id.notin_(skip) if skip else true())
            .order_by(func.least(Account.incremental_due_at, Account.reconcile_due_at), Account.account_id)
            .limit(1)).first()
        if picked is None:
            return None
        account = picked[0]
        try:
            runtime = store._runtime(s, tenant, account)
        except Refused:
            return account
        # The runtime lock serializes claimers; recheck the lease under it.
        row = s.get(Account, account, with_for_update=True)
        if (row is None or not row.enabled or (row.lease_token is not None and row.lease_until > now)):
            return account
        if row.binding_id != runtime.binding_id:
            _park(s, row, runtime, now, "binding_changed")
            return account
        connection = s.get(OfProviderConnection, account)
        if not _auth_ok(runtime, connection, now):
            if row.status != "paused_auth" or row.blocked_reason != "connection_required":
                row.status, row.blocked_reason, row.updated_at = "paused_auth", "connection_required", now
            return account
        if row.status == "paused_auth":
            # Reconnected: resume from the durable cursors, nothing is reset.
            row.status, row.blocked_reason = "active", None
        row.connection_revision = connection.revision
        ops = {op.kind: op for op in s.scalars(select(Operation).where(Operation.account_id == account,
            Operation.kind.in_(PASS_KINDS), or_(Operation.status == "queued",
                and_(Operation.status == "running", Operation.lease_until <= now))).with_for_update())}
        op = next((ops[k] for k in PASS_KINDS if k in ops), None)
        if op is None:
            if row.incremental_due_at is not None and row.incremental_due_at <= now:
                kind, due = "incremental", row.incremental_due_at
            elif row.reconcile_due_at is not None and row.reconcile_due_at <= now:
                kind, due = "reconcile", row.reconcile_due_at
            else:
                return account
            op = Operation(account_id=account, operation_id=str(uuid.uuid4()), binding_id=runtime.binding_id,
                command_key=_scheduled_key(kind, account, runtime.binding_id, due), request_hash=digest({"schedule": kind}),
                actor=row.actor, kind=kind, status="queued", coverage_json="{}", result_json="{}", attempts=0,
                created_at=now, updated_at=now)
            s.add(op)
        sync_kind = op.kind if op.kind in SYNC_KINDS else json.loads(op.result_json).get("syncKind", "incremental")
        token = str(uuid.uuid4())
        op.status, op.lease_token, op.lease_until = "running", token, now + timedelta(seconds=LEASE_SECONDS)
        op.connection_revision, op.attempts, op.updated_at = connection.revision, op.attempts + 1, now
        row.lease_token, row.lease_until, row.updated_at = token, op.lease_until, now
        s.flush()
        return Claim(account, runtime.binding_id, op.operation_id, token, op.kind, sync_kind, connection.revision, now)


def _check(store, tenant, s, claim, *, write=True):
    """Revalidate the leased scope inside a transaction; StaleClaim means write nothing."""
    try:
        runtime = store._runtime(s, tenant, claim.account)
    except Refused:
        raise StaleClaim() from None
    row = s.get(Account, claim.account, with_for_update=True)
    op = s.get(Operation, (claim.account, claim.operation_id), with_for_update=True)
    connection = s.get(OfProviderConnection, claim.account)
    if (row is None or not row.enabled or row.lease_token != claim.token or row.binding_id != claim.binding_id
            or runtime.binding_id != claim.binding_id or op is None or op.status != "running"
            or op.lease_token != claim.token or connection is None or connection.binding_id != claim.binding_id
            or connection.revision != claim.connection_revision or not _auth_ok(runtime, connection, claim.now)):
        raise StaleClaim()
    return runtime, row, op


def process_due(store, tenant, *, limit=1, stop=None, now=None):
    """Run at most `limit` leased passes; returns how many ran. Provider reads are never made here."""
    _worker_guard(tenant, limit)
    processed, skipped = 0, set()
    for _ in range(limit):
        claim = None
        for _attempt in range(16):
            if stop is not None and stop.is_set():
                return processed
            picked = claim_due(store, tenant, now=now, skip=skipped)
            if picked is None or isinstance(picked, Claim):
                claim = picked
                break
            skipped.add(picked)
        if claim is None:
            break
        run_claim(store, tenant, claim)
        processed += 1
    return processed


def run_claim(store, tenant, claim):
    """Execute one leased pass; returns complete | partial | failed | stale | paused."""
    try:
        outcome = _incremental(store, tenant, claim) if claim.sync_kind == "incremental" else _reconcile(store, tenant, claim)
        return _finish(store, tenant, claim, outcome)
    except StaleClaim:
        _abandon(store, claim, "scope_changed")
        return "stale"
    except Exception:
        _fail(store, tenant, claim)
        return "failed"


def _finish(store, tenant, claim, outcome):
    now = claim.now
    with Session(store.engine) as s, s.begin():
        runtime, row, op = _check(store, tenant, s, claim)
        status, backlog = outcome["status"], outcome.get("backlog", row.backlog)
        op.status, op.completed_at, op.updated_at = status, now, now
        op.coverage_json, op.result_json = canonical(outcome["coverage"]), canonical(
            {**json.loads(op.result_json), **outcome["result"]})
        op.last_error = outcome.get("error")
        op.lease_token = op.lease_until = row.lease_token = row.lease_until = None
        row.updated_at = now
        if status == "cancelled":
            # Auth was lost mid-pass: no schedule change, resume after reconnection.
            row.status, row.blocked_reason = "paused_auth", "connection_required"
            return "paused"
        row.failures = 0
        if claim.sync_kind == "incremental":
            row.last_incremental_at, row.backlog = now, backlog
            row.incremental_due_at = now + timedelta(seconds=DRAIN_SECONDS) if backlog > 0 else _next_incremental(claim.account, now)
        else:
            row.last_reconcile_at = now
            row.reconcile_due_at = now + timedelta(seconds=RECONCILE_SECONDS)
            row.reconcile_after_item_id = outcome["nextAfter"]
            row.reconcile_cycle_started_at = None if outcome["wrapped"] else (row.reconcile_cycle_started_at or now)
            if outcome["wrapped"]:
                row.last_full_reconcile_at = now
        _sync_initial_operation(s, claim.account, row, now)
        return status


def _sync_initial_operation(s, account, row, now):
    job = s.get(OfOfferImportJob, (account, row.initial_job_id)) if row.initial_job_id else None
    if job is None or job.status in {"pending", "running"}:
        return
    mapped = {"complete": "complete", "partial": "partial", "failed": "failed"}.get(job.status, "cancelled")
    for op in s.scalars(select(Operation).where(Operation.account_id == account, Operation.kind == "initial",
            Operation.status == "queued", Operation.result_json.contains(job.job_id)).with_for_update()):
        op.status, op.completed_at, op.updated_at, op.last_error = mapped, job.completed_at or now, now, job.last_error


def _abandon(store, claim, reason):
    """Release a stale lease without touching the schedule or any result."""
    now = utcnow()
    with Session(store.engine) as s, s.begin():
        op = s.get(Operation, (claim.account, claim.operation_id), with_for_update=True)
        if op is not None and op.lease_token == claim.token and op.status == "running":
            op.status, op.last_error, op.lease_token, op.lease_until = "cancelled", reason, None, None
            op.updated_at = op.completed_at = now
        row = s.get(Account, claim.account, with_for_update=True)
        if row is not None and row.lease_token == claim.token:
            row.lease_token = row.lease_until = None


def _fail(store, tenant, claim):
    """Record an unexpected failure and back off with jitter (capped)."""
    now = claim.now
    try:
        with Session(store.engine) as s, s.begin():
            runtime, row, op = _check(store, tenant, s, claim)
            row.failures += 1
            delay = min(INCREMENTAL_SECONDS * 2 ** min(row.failures, 6), BACKOFF_CAP_SECONDS)
            due = now + timedelta(seconds=delay + jitter(claim.account, now))
            if claim.sync_kind == "incremental":
                row.incremental_due_at = due
            else:
                row.reconcile_due_at = due
            op.status, op.last_error, op.completed_at, op.updated_at = "failed", "pass_failed", now, now
            op.lease_token = op.lease_until = row.lease_token = row.lease_until = None
            row.updated_at = now
    except StaleClaim:
        _abandon(store, claim, "scope_changed")


def _queue_refresh(store, tenant, claim, actor, sources):
    """Enqueue one bounded refresh job for sources lacking observations (provider reads stay in its worker)."""
    from .offer_import import InvalidImport, request_job
    if not sources:
        return "none"
    try:
        request_job(store, tenant, claim.account, mode="refresh", command_key=digest(["refresh", claim.operation_id]),
            actor=actor, sources=[{"fanId": str(f), "messageId": str(m)} for f, m in sources])
        return "queued"
    except Refused as exc:
        if str(exc) == "verified connection unavailable":
            return "auth"
        # An active refresh (manual or earlier) already owns the account's extra page budget.
        return "active" if str(exc) == "import job already active" else "unavailable"
    except (InvalidImport, IntegrityError):
        return "unavailable"


def _queue_vault(store, tenant, claim, actor):
    from .vault_management import InvalidVault, request_sync
    with Session(store.engine) as s:
        latest = s.scalar(select(func.max(OfVaultSyncJob.requested_at)).where(OfVaultSyncJob.account_id == claim.account))
    if latest is not None and (claim.now - latest).total_seconds() < VAULT_MIN_INTERVAL_SECONDS:
        return "recent"
    try:
        request_sync(store, tenant, claim.account, expected_connection_revision=claim.connection_revision,
            command_key=digest(["vault", claim.operation_id]), actor=actor)
        return "queued"
    except Refused as exc:
        return "auth" if str(exc) == "verified connection unavailable" else "active" if "already active" in str(exc) else "unavailable"
    except (InvalidVault, IntegrityError):
        return "unavailable"


def _fold(store, tenant, claim):
    """DB-only fold of new outgoing paid messages into the initial job's candidates."""
    from .offer_import import _date as day, _merge
    folded, lacking, last = 0, {}, None
    for _ in range(FOLD_BATCHES):
        with Session(store.engine) as s, s.begin():
            runtime, row, op = _check(store, tenant, s, claim)
            job = _ensure_initial(s, runtime, row, claim.now, claim.operation_id)
            rows = s.execute(select(Message.fan_id, Message.message_id, Message.media_ids, Message.price_cents,
                    Message.created_at).where(Message.account_id == claim.account, Message.direction == "out",
                    Message.price_cents > 0, Message.message_id > (row.covered_through_message_id or 0))
                .order_by(Message.message_id, Message.fan_id).limit(FOLD_ROWS)).all()
            ids = [r[1] for r in rows]
            echoes = echo_map(s, claim.account, runtime.binding_id, ids)
            seen = set(s.scalars(select(OfOfferObservation.source_id).where(OfOfferObservation.account_id == claim.account,
                OfOfferObservation.binding_id == runtime.binding_id, OfOfferObservation.source_kind == "message",
                OfOfferObservation.source_id.in_(ids)))) if ids else set()
            touched = {}
            for fan, mid, raw, price, created in rows:
                media = _ids(raw)
                if not media or mid in echoes:
                    continue
                mkey = media_key(media)
                candidate = touched.get(mkey) or s.get(OfOfferImportCandidate, (claim.account, job.job_id, mkey), with_for_update=True)
                if candidate is None:
                    candidate = OfOfferImportCandidate(account_id=claim.account, job_id=job.job_id, media_key=mkey,
                        media_json=canonical(sorted(media)), source_count=0, variants_json="[]", samples_json="[]",
                        revision=0, updated_at=claim.now)
                    s.add(candidate)
                touched[mkey] = candidate
                _merge(candidate, media, price, {"fanId": str(fan), "messageId": mid, "priceCents": price, "sentAt": day(created)})
                candidate.updated_at = claim.now
                if mid not in seen:
                    lacking[(fan, mid)] = None
            if rows:
                row.covered_through_message_id = rows[-1][1]
                last = rows[-1][1]
            row.updated_at = claim.now
            folded += len(rows)
            more = len(rows) == FOLD_ROWS
        if not more:
            break
    with Session(store.engine) as s, s.begin():
        runtime, row, op = _check(store, tenant, s, claim)
        backlog = s.scalar(select(func.count()).select_from(select(Message.message_id).where(
            Message.account_id == claim.account, Message.direction == "out", Message.price_cents > 0,
            Message.message_id > (row.covered_through_message_id or 0)).limit(BACKLOG_PROBE).subquery())) or 0
        conflicts = detect_conflicts(s, claim.account, runtime.binding_id, claim.now)
        actor = row.actor
        covered = row.covered_through_message_id
    return folded, lacking, backlog, conflicts, actor, covered, last


def _incremental(store, tenant, claim):
    folded, lacking, backlog, conflicts, actor, covered, _ = _fold(store, tenant, claim)
    wanted = sorted(lacking, key=lambda t: t[1], reverse=True)
    sources = wanted[:REFRESH_SOURCES]
    refresh = _queue_refresh(store, tenant, claim, actor, sources)
    vault = _queue_vault(store, tenant, claim, actor)
    truncated = len(wanted) > REFRESH_SOURCES
    coverage = {"foldedRows": folded, "foldedThroughMessageId": None if covered is None else str(covered),
        "backlog": backlog, "bounded": backlog > 0, "newSourcesWithoutDetails": len(wanted),
        "refreshSources": len(sources), "refresh": refresh, "refreshTruncated": truncated, "vaultRead": vault,
        "conflictsOpened": conflicts}
    result = {"syncKind": "incremental"} if claim.kind == "sync_now" else {}
    if "auth" in (refresh, vault):
        return {"status": "cancelled", "error": "connection_unavailable", "coverage": coverage, "result": result}
    status = "partial" if backlog > 0 or truncated else "complete"
    return {"status": status, "backlog": backlog, "coverage": coverage, "result": result}


def _reconcile(store, tenant, claim):
    """Bounded daily pass: enabled + conflicted items first, then a rotating slice of the rest."""
    with Session(store.engine) as s, s.begin():
        runtime, row, op = _check(store, tenant, s, claim)
        account, binding, actor = claim.account, runtime.binding_id, row.actor
        base = (CatalogItem.account_id == account, CATALOG_IS_SINGLE, CatalogItem.pricing_mode == "fixed")
        total = s.scalar(select(func.count()).select_from(CatalogItem).where(*base)) or 0
        conflicted = exists().where(Conflict.account_id == account, Conflict.item_id == CatalogItem.id, Conflict.status == "open")
        priority_all = s.scalar(select(func.count()).select_from(CatalogItem).where(*base, or_(CatalogItem.enabled.is_(True), conflicted))) or 0
        priority = list(s.scalars(select(CatalogItem.id).where(*base, or_(CatalogItem.enabled.is_(True), conflicted))
            .order_by(conflicted.desc(), CatalogItem.id).limit(PRIORITY_QUOTA)))
        after = row.reconcile_after_item_id or 0
        rotation = list(s.scalars(select(CatalogItem.id).where(*base, CatalogItem.id > after)
            .order_by(CatalogItem.id).limit(ROTATION_QUOTA + 1)))
        # One extra row proves whether items remain; the rotation wraps only when none do.
        wrapped, rotation = len(rotation) <= ROTATION_QUOTA, rotation[:ROTATION_QUOTA]
        checked = list(dict.fromkeys(priority + rotation))
        connection = s.get(OfProviderConnection, account)
        unusable, unverified, unavailable, sources = [], 0, [], []
        for item_id in checked:
            item = s.get(CatalogItem, item_id)
            states = _media_states(s, account, runtime, connection, _ids(item.media_ids) or [])
            if "unusable" in states.values():
                unusable.append(item_id)
            elif "ok" not in states.values() or "unverified" in states.values():
                unverified += 1
            selected = selected_observation(s, account, binding, item_id)
            if selected is None or selected.source_kind != "message" or selected.fan_id is None:
                continue
            if source_state(s, account, selected) == "unavailable":
                unavailable.append(item_id)
            elif (selected.fan_id, selected.source_id) not in sources and s.get(Message, (account, selected.fan_id, selected.source_id)) is not None \
                    and echo_item(s, account, binding, selected.source_id) is None:
                sources.append((selected.fan_id, selected.source_id))
        conflicts = detect_conflicts(s, account, binding, claim.now)
    refresh = _queue_refresh(store, tenant, claim, actor, sources[:REFRESH_SOURCES])
    coverage = {"itemsChecked": len(checked), "priorityChecked": len(priority), "rotationChecked": len(rotation),
        "rotationFromItemId": rotation[0] if rotation else None, "rotationThroughItemId": rotation[-1] if rotation else None,
        "totalItems": total, "priorityTotal": priority_all, "wrapped": wrapped, "refreshSources": len(sources[:REFRESH_SOURCES]),
        "refresh": refresh, "refreshTruncated": len(sources) > REFRESH_SOURCES, "conflictsOpened": conflicts,
        "note": "Items and their selected sources only; historical messages are not re-read."}
    result = {"unusableItems": [str(i) for i in unusable[:100]], "unverifiedItems": unverified,
        "sourceUnavailableItems": [str(i) for i in unavailable[:100]]}
    if claim.kind == "sync_now":
        result["syncKind"] = "reconcile"
    if refresh == "auth":
        return {"status": "cancelled", "error": "connection_unavailable", "coverage": coverage, "result": result}
    return {"status": "complete" if wrapped else "partial", "coverage": coverage, "result": result,
        "nextAfter": None if wrapped else rotation[-1], "wrapped": wrapped}


# ── Sync now, operations ────────────────────────────────────────────────

def operation_view(s, row):
    if row.kind in ("upload", "retry"):
        try:
            from . import content_sync_media as media
        except ImportError:
            media = None
        if media is not None and row.asset_id:
            from db.onlyfans_offer_import_models import OfContentSyncAsset
            return media.operation_view(row, s.get(OfContentSyncAsset, (row.account_id, row.asset_id)))
    result, coverage, status, completed = json.loads(row.result_json), json.loads(row.coverage_json), row.status, row.completed_at
    retryable = (row.kind in PASS_KINDS and "retriedBy" not in result and (
        (status == "failed" and row.last_error in RETRYABLE_ERRORS) or (status == "cancelled" and row.last_error == "connection_unavailable")))
    if row.kind == "initial":
        job = s.get(OfOfferImportJob, (row.account_id, result.get("jobId", "")))
        if job is not None:
            status = {"pending": "queued", "running": "running", "complete": "complete", "partial": "partial",
                "failed": "failed"}.get(job.status, "cancelled")
            completed = job.completed_at
            coverage = {"source": "imported_history", "rowsScanned": job.rows_scanned, "pagesRead": job.pages_read,
                "pageBudget": job.page_budget, "complete": job.status == "complete"}
    return {"operationId": row.operation_id, "kind": row.kind, "status": status, "createdAt": _date(row.created_at),
        "updatedAt": _date(row.updated_at), "completedAt": _date(completed), "coverage": coverage, "result": result,
        "lastError": row.last_error, "retryable": retryable, "draftId": row.draft_id, "assetId": row.asset_id}


def request_sync_now(store, tenant, account, *, kind, command_key, actor):
    """Queue the same bounded work as the schedule. Returns the active operation instead of a duplicate."""
    _identity(command_key, actor)
    if kind not in SYNC_KINDS:
        raise InvalidSync("Choose an incremental or reconcile pass.")
    request_hash = digest({"action": "sync", "kind": kind})
    now = utcnow()
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        prior = s.scalar(select(Operation).where(Operation.account_id == account, Operation.command_key == command_key))
        if prior is not None:
            if (prior.request_hash, prior.actor, prior.binding_id) != (request_hash, actor, runtime.binding_id) or prior.kind != "sync_now":
                raise Refused("sync idempotency conflict")
            return {"operation": operation_view(s, prior)}
        _key_taken(s, account, command_key, Operation)
        row = s.get(Account, account, with_for_update=True)
        if row is None or not row.enabled or row.binding_id != runtime.binding_id:
            raise Refused("sync not enabled")
        _connection(s, runtime)
        active = s.scalar(select(Operation).where(Operation.account_id == account, Operation.status.in_(("queued", "running")),
            or_(Operation.kind == "sync_now", Operation.kind == kind)).order_by(Operation.created_at).limit(1))
        if active is not None:
            return {"operation": operation_view(s, active)}
        op = Operation(account_id=account, operation_id=str(uuid.uuid4()), binding_id=runtime.binding_id,
            command_key=command_key, request_hash=request_hash, actor=actor, kind="sync_now", status="queued",
            connection_revision=row.connection_revision, coverage_json="{}", result_json=canonical({"syncKind": kind}),
            attempts=0, created_at=now, updated_at=now)
        s.add(op)
        s.flush()
        return {"operation": operation_view(s, op)}


def list_operations(store, tenant, account, *, after=None, limit=25):
    if type(limit) is not int or not 1 <= limit <= 50:
        raise InvalidSync("Use a page size between 1 and 50.")
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        query = select(Operation).where(Operation.account_id == account, Operation.binding_id == runtime.binding_id)
        if after is not None:
            marker = s.get(Operation, (account, _uuid(after)))
            if marker is None:
                raise InvalidSync("That page reference is invalid.")
            query = query.where(or_(Operation.created_at < marker.created_at,
                and_(Operation.created_at == marker.created_at, Operation.operation_id < marker.operation_id)))
        rows = s.scalars(query.order_by(Operation.created_at.desc(), Operation.operation_id.desc()).limit(limit + 1)).all()
        return {"items": [operation_view(s, r) for r in rows[:limit]],
            "nextCursor": rows[limit - 1].operation_id if len(rows) > limit else None}


def get_operation(store, tenant, account, operation_id):
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        row = s.get(Operation, (account, _uuid(operation_id)))
        if row is None or row.binding_id != runtime.binding_id:
            raise Refused("sync operation unavailable")
        return {"operation": operation_view(s, row)}


def retry_operation(store, tenant, account, operation_id, *, command_key, actor):
    """Retry only a classified retryable outcome; unknown outcomes are reconciled, never replayed."""
    _identity(command_key, actor)
    _uuid(operation_id)
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        row = s.get(Operation, (account, operation_id))
        kind = None if row is None or row.binding_id != runtime.binding_id else row.kind
    if kind is None:
        raise Refused("sync operation unavailable")
    if kind in ("upload", "retry"):
        from . import content_sync_media
        return {"operation": content_sync_media.retry_asset(store, tenant, account, operation_id, command_key=command_key, actor=actor)}
    request_hash = digest({"action": "retry", "operationId": operation_id})
    now = utcnow()
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        prior = s.get(Decision, (account, command_key))
        if prior is not None:
            if (prior.action, prior.request_hash, prior.actor, prior.binding_id) != ("retry", request_hash, actor, runtime.binding_id):
                raise Refused("sync idempotency conflict")
            return json.loads(prior.result_json)
        _key_taken(s, account, command_key, Decision)
        original = s.get(Operation, (account, operation_id), with_for_update=True)
        sync = s.get(Account, account, with_for_update=True)
        if not operation_view(s, original)["retryable"]:
            raise Refused("sync operation not retryable")
        if sync is None or not sync.enabled or sync.binding_id != runtime.binding_id:
            raise Refused("sync not enabled")
        _connection(s, runtime)
        sync_kind = json.loads(original.result_json).get("syncKind", original.kind)
        sync_kind = sync_kind if sync_kind in SYNC_KINDS else "incremental"
        active = s.scalar(select(Operation).where(Operation.account_id == account, Operation.status.in_(("queued", "running")),
            or_(Operation.kind == "sync_now", Operation.kind == sync_kind)).limit(1))
        if active is not None:
            fresh = active
        else:
            fresh = Operation(account_id=account, operation_id=str(uuid.uuid4()), binding_id=runtime.binding_id,
                command_key=digest(["retry-operation", account, command_key]), request_hash=request_hash, actor=actor,
                kind="sync_now", status="queued", connection_revision=sync.connection_revision, coverage_json="{}",
                result_json=canonical({"syncKind": sync_kind, "retryOf": operation_id}), attempts=0, created_at=now, updated_at=now)
            s.add(fresh)
        original.result_json = canonical({**json.loads(original.result_json), "retriedBy": fresh.operation_id})
        original.updated_at = now
        s.flush()
        result = {"operation": operation_view(s, fresh)}
        s.add(Decision(account_id=account, command_key=command_key, binding_id=runtime.binding_id, action="retry",
            request_hash=request_hash, actor=actor, subject_id=operation_id, before_json=canonical({"status": original.status,
            "lastError": original.last_error}), after_json=canonical({"operationId": fresh.operation_id}),
            result_json=canonical(result), created_at=now))
        return result


# ── Conflicts ───────────────────────────────────────────────────────────

def _counts(raw):
    ids = _ids(raw)
    return None if ids is None else len(ids)


def conflict_view(s, row):
    item = s.get(CatalogItem, row.item_id)
    new = s.get(OfOfferObservation, (row.account_id, row.observation_id))
    prior = s.scalar(select(OfOfferObservation).where(OfOfferObservation.account_id == row.account_id,
        OfOfferObservation.binding_id == row.binding_id, OfOfferObservation.source_kind == new.source_kind,
        OfOfferObservation.source_id == new.source_id, OfOfferObservation.fingerprint == row.prior_fingerprint))
    previews = _ids(item.preview_media_ids) or []
    return {"conflictId": row.conflict_id, "itemId": str(row.item_id), "revision": row.revision, "status": row.status,
        "createdAt": _date(row.created_at),
        "current": {"priceCents": item.price_cents, "mediaCount": len(_ids(item.media_ids) or []),
            "previewCount": len(previews), "sourceAt": _date(prior.source_at) if prior else None},
        "source": {"priceCents": new.price_cents, "mediaCount": _counts(new.media_json),
            "previewCount": None if new.preview_json is None else _counts(new.preview_json),
            "sourceAt": _date(new.source_at), "lockedText": new.locked_text},
        "itemFingerprint": item_fingerprint(item), "resolution": row.resolution, "resolvedAt": _date(row.resolved_at)}


def list_conflicts(store, tenant, account, *, status="open"):
    if status not in {"open", "all"}:
        raise InvalidSync("Use open or all conflicts.")
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        query = select(Conflict).where(Conflict.account_id == account, Conflict.binding_id == runtime.binding_id)
        if status == "open":
            query = query.where(Conflict.status == "open")
        rows = s.scalars(query.order_by(Conflict.created_at.desc(), Conflict.conflict_id).limit(200)).all()
        return {"items": [conflict_view(s, r) for r in rows]}


def _terms(item):
    return {"priceCents": item.price_cents, "mediaIds": _ids(item.media_ids), "previewMediaIds": _ids(item.preview_media_ids),
        "enabled": item.enabled}


def _apply_source(s, runtime, item, observation):
    """Move an item to a source's exact reviewed terms through the same checks as creation; stays fixed."""
    media = _ids(observation.media_json)
    previews = None if observation.preview_json is None else _ids(observation.preview_json)
    if (media is None or previews is None or json.loads(observation.reasons_json) or observation.locked_text is not False
            or observation.currency != "USD" or not MIN_PRICE_CENTS <= observation.price_cents <= MAX_PRICE_CENTS
            or not 1 <= len(media) <= MAX_ATTACHMENTS or set(previews) >= set(media)):
        raise Refused("source not ready")
    connection = _connection(s, runtime)
    from .vault_management import _ready
    rows = {}
    for mid in media:
        row = s.scalar(select(VaultItem).where(VaultItem.account_id == item.account_id, VaultItem.media_id == mid).with_for_update(read=True))
        observed = s.get(OfVaultObservation, (item.account_id, mid))
        job = s.get(OfVaultSyncJob, (item.account_id, observed.job_id)) if observed else None
        if row is None or not _ready(row, observed, job, connection, runtime.binding_id)[0]:
            raise Refused("vault media unavailable")
        if mid in previews and row.kind != "photo":
            raise Refused("source not ready")
        rows[mid] = row
    for other_id, raw in s.execute(select(CatalogItem.id, CatalogItem.media_ids).where(CatalogItem.account_id == item.account_id,
            CATALOG_IS_SINGLE, CatalogItem.id != item.id).limit(5001)).all():
        ids = _ids(raw)
        if ids is not None and sorted(ids) == sorted(media):
            raise Refused("catalogue assets already present")
    videos = [rows[m] for m in media if m not in previews and rows[m].kind == "video"]
    item.price_cents, item.pricing_mode = observation.price_cents, "fixed"
    item.media_ids, item.preview_media_ids = canonical(media), canonical(previews)
    item.kind = "video" if videos else "image" if len(media) == 1 else "image_set"
    item.duration_sec = videos[0].duration_seconds if len(videos) == 1 else None


def resolve_conflict(store, tenant, account, conflict_id, *, resolution, expected_revision, expected_item_fingerprint,
                     command_key, actor):
    """One explicit, versioned, audited decision; never picks the latest price automatically."""
    _identity(command_key, actor)
    _uuid(conflict_id)
    if (resolution not in {"accept_source", "keep_crm", "disable"} or type(expected_revision) is not int
            or not 1 <= expected_revision < 2**31 - 1 or type(expected_item_fingerprint) is not str
            or not re.fullmatch(r"[a-f0-9]{64}", expected_item_fingerprint)):
        raise InvalidSync("Choose a resolution with the conflict's current revision and item fingerprint.")
    request_hash = digest({"action": "resolve", "conflictId": conflict_id, "resolution": resolution,
        "expectedRevision": expected_revision, "expectedItemFingerprint": expected_item_fingerprint})
    now = utcnow()
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        prior = s.get(Decision, (account, command_key))
        if prior is not None:
            if (prior.action, prior.request_hash, prior.actor, prior.binding_id) != ("resolve", request_hash, actor, runtime.binding_id):
                raise Refused("sync idempotency conflict")
            return json.loads(prior.result_json)
        _key_taken(s, account, command_key, Decision)
        conflict = s.get(Conflict, (account, conflict_id), with_for_update=True)
        if conflict is None or conflict.binding_id != runtime.binding_id:
            raise Refused("sync conflict unavailable")
        item = s.scalar(select(CatalogItem).where(CatalogItem.account_id == account, CatalogItem.id == conflict.item_id,
            CATALOG_IS_SINGLE).with_for_update())
        if item is None:
            raise Refused("sync conflict unavailable")
        if conflict.status != "open" or conflict.revision != expected_revision or item_fingerprint(item) != expected_item_fingerprint:
            raise Refused("sync revision conflict")
        observation = s.get(OfOfferObservation, (account, conflict.observation_id))
        before_item, before_fingerprint = _terms(item), item_fingerprint(item)
        if resolution == "accept_source":
            _apply_source(s, runtime, item, observation)
        elif resolution == "disable":
            item.enabled = False
        if resolution != "keep_crm" and _terms(item) != before_item:
            item.updated_at = now
            s.flush()
            revision = _revision(s, account, item.id)
            s.add(OfContentChange(account_id=account, item_id=item.id, command_key=command_key, previous_revision=revision,
                revision=revision + 1, fingerprint_before=before_fingerprint, fingerprint_after=item_fingerprint(item),
                request_hash=request_hash, actor=actor, before_json=canonical(before_item), after_json=canonical(_terms(item)),
                created_at=now))
        before_conflict = {"status": conflict.status, "revision": conflict.revision}
        conflict.status, conflict.resolution, conflict.resolved_by, conflict.resolved_command_key = "resolved", resolution, actor, command_key
        conflict.resolved_at = conflict.updated_at = now
        conflict.revision += 1
        s.flush()
        result = {"conflict": conflict_view(s, conflict), "itemId": str(item.id)}
        s.add(Decision(account_id=account, command_key=command_key, binding_id=runtime.binding_id, action="resolve",
            request_hash=request_hash, actor=actor, subject_id=conflict_id,
            before_json=canonical({"conflict": before_conflict, "item": before_item, "sourceObservationId": conflict.observation_id}),
            after_json=canonical({"resolution": resolution, "item": _terms(item), "revision": conflict.revision}),
            result_json=canonical(result), created_at=now))
        return result


# ── Commands ────────────────────────────────────────────────────────────

def get_command(store, tenant, account, command_key):
    """Resolve a lost acknowledgement from whichever table recorded the command."""
    if type(command_key) is not str or not re.fullmatch(r"[a-f0-9]{64}", command_key):
        raise InvalidSync("That command reference is invalid.")
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        decision = s.get(Decision, (account, command_key))
        if decision is not None and decision.binding_id == runtime.binding_id:
            return {"commandKey": command_key, "recorded": True, "action": decision.action, **json.loads(decision.result_json)}
        op = s.scalar(select(Operation).where(Operation.account_id == account, Operation.command_key == command_key,
            Operation.binding_id == runtime.binding_id))
        if op is not None:
            return {"commandKey": command_key, "recorded": True, "action": "sync", "operation": operation_view(s, op)}
        draft = s.scalar(select(Draft).where(Draft.account_id == account, Draft.command_key == command_key,
            Draft.binding_id == runtime.binding_id))
        if draft is not None:
            return {"commandKey": command_key, "recorded": True, "action": "save", "draftId": draft.draft_id,
                "state": draft.state, "revision": draft.revision}
        return {"commandKey": command_key, "recorded": False}
