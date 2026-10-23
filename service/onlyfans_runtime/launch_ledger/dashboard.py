"""Dashboard view and retry command for the ledger scan (contract of-ledger.v1).

Neither function contacts the provider. A retry only makes a held scan due
(next_poll_at); the existing worker re-reads the frozen window/offset. It never
sets status idle, writes lastCompleteAt, clears the error or touches evidence,
conflicts, transactions or ownership. Callers hold the agency scope.
"""
from datetime import timedelta
import hashlib
import hmac
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import OfCreatorBinding, Transaction
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_ledger_models import OfLedgerEvidence, OfLedgerScan
from db.onlyfans_models import OfRuntime
from onlyfans_read.contract import iso
from .. import ledger
from ..native_context import canonical
from ..store import Refused, utcnow
from .models import LedgerRetryCommand

SCHEMA = "of-ledger.v1"
# Static public codes only; anything else (a future or corrupt value) is reported as unknown_error.
ERRORS = frozenset({"connection_unavailable", "connection_changed", "unsupported_ledger_response",
                    "unsupported_ledger_pagination", "ledger_read_failed", "provider_read_deferred",
                    "provider_read_stale", "provider_rate_limited"})
INVESTIGATE = frozenset({"unsupported_ledger_response", "unsupported_ledger_pagination"})
KEY = re.compile(r"[a-f0-9]{64}")


def fingerprint(runtime, connection, scan):
    """Opaque CAS token over nonsecret scan/connection metadata; any worker change invalidates it."""
    if scan is None:
        return None
    snapshot = dict(schema=SCHEMA, runtime_binding=runtime.binding_id,
        connection=None if connection is None else {key: getattr(connection, key) for key in (
            "binding_id", "owner_id", "revision", "status", "verified_at", "expires_at", "last_error")},
        scan={column.name: getattr(scan, column.name) for column in OfLedgerScan.__table__.columns})
    return hashlib.sha256(canonical(snapshot).encode()).hexdigest()


def read_ledger(s, tenant, account, *, now=None):
    """The selected account only; None (non-disclosing) when it is not this tenant's live binding."""
    from ..commerce import held
    now = now or utcnow()
    binding = select(OfCreatorBinding).where(OfCreatorBinding.account_id == account,
        OfCreatorBinding.channel == "onlyfans", OfCreatorBinding.revoked_at.is_(None))
    if tenant is not None:
        binding = binding.where(OfCreatorBinding.tenant_id == tenant)
    binding = s.scalar(binding.with_for_update(read=True))
    if binding is None:
        return None
    runtime, connection, scan = s.get(OfRuntime, account), s.get(OfProviderConnection, account), s.get(OfLedgerScan, account)
    if runtime is None or runtime.binding_id != binding.id:
        return None
    # A replaced creator binding must never reveal its predecessor's window,
    # cursor, diagnostics or evidence counts to the newly assigned creator.
    if scan is not None and scan.binding_id != runtime.binding_id:
        scan = None
    if connection is not None and connection.binding_id != runtime.binding_id:
        connection = None
    from ..launch_operations import _ledger_core
    core = _ledger_core(s, runtime, now=now)
    conflicts, unlinked = core["conflictingEvidence"], core["unresolvedTransactions"]
    error = None if scan is None or scan.last_error is None else scan.last_error if scan.last_error in ERRORS else "unknown_error"
    return {"schemaVersion": SCHEMA, "accountId": account,
        "status": "not_started" if scan is None else scan.status,
        "windowStart": scan and iso(scan.window_start), "windowEnd": scan and iso(scan.window_end),
        "pageOffset": scan and scan.page_offset,
        "lastCompleteAt": scan and iso(scan.last_complete_at), "lastFullAt": scan and iso(scan.last_full_at),
        "nextPollAt": scan and iso(scan.next_poll_at), "lastError": error,
        "leaseActive": bool(scan and scan.lease_until is not None and scan.lease_until > now),
        "connectionCurrent": ledger._current(runtime, connection, scan, now),
        "connectionRevision": None if connection is None else connection.revision,
        "conflictCount": conflicts, "unlinkedPaidCount": unlinked,
        "requiresInvestigation": bool(scan and scan.status == "held" and scan.last_error in INVESTIGATE or conflicts or unlinked),
        "commerceHeld": held(s, account), "scanFingerprint": fingerprint(runtime, connection, scan)}


def retry(store, tenant, account, *, command_key, expected_revision, expected_fingerprint, actor):
    """Record the command, then make the held scan due, in one transaction. Never a reconciliation."""
    from onlyfans_read.agency_scope import actor_allowed
    if (type(command_key) is not str or not KEY.fullmatch(command_key) or type(expected_revision) is not int
            or not 0 <= expected_revision < 2**31 - 1 or type(expected_fingerprint) is not str
            or not KEY.fullmatch(expected_fingerprint) or not actor_allowed(actor)):
        raise Refused("invalid ledger retry request")
    request_hash = hashlib.sha256(canonical({"expectedConnectionRevision": expected_revision,
        "expectedScanFingerprint": expected_fingerprint}).encode()).hexdigest()
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)  # runtime lock, live binding and agency grant
        prior = s.get(LedgerRetryCommand, (account, command_key))
        if prior is not None:
            if (prior.request_hash, prior.actor, prior.binding_id) != (request_hash, actor, runtime.binding_id):
                raise Refused("idempotency key conflict")
            return ack(prior)
        now = utcnow()
        connection = s.get(OfProviderConnection, account)
        scan = s.get(OfLedgerScan, account, with_for_update=True)
        if scan is None or not ledger._current(runtime, connection, scan, now):
            raise Refused("ledger state changed")
        if scan.lease_until is not None and scan.lease_until > now:
            raise Refused("ledger busy")
        if scan.status != "held":
            raise Refused("ledger not held")
        if (connection.revision != expected_revision
                or not hmac.compare_digest(fingerprint(runtime, connection, scan), expected_fingerprint)):
            raise Refused("ledger state changed")
        command = LedgerRetryCommand(account_id=account, command_key=command_key, binding_id=runtime.binding_id,
            request_hash=request_hash, actor=actor, expected_connection_revision=expected_revision,
            expected_scan_fingerprint=expected_fingerprint, result="queued", created_at=now)
        s.add(command)
        s.flush()  # durable evidence precedes scheduling
        scan.next_poll_at = now
        return ack(command)


def ack(command):
    return {"accepted": True, "outcome": "queued", "commandKey": command.command_key,
            "accountId": command.account_id, "queuedAt": iso(command.created_at)}
