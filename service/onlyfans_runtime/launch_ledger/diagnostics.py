"""Bounded, redacted ledger diagnostics for the existing worker context.

These helpers never apply transactions, advance cursors, claim leases, retry a
request or change account activation. A valid diagnostic is not reconciliation.
The operator must coordinate the read with the existing ledger worker; a state
change during the request discards the response. No executable credential
discovery entry point is provided.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os

from sqlalchemy.orm import Session

from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_ledger_models import OfLedgerScan
from onlyfans_read.contract import ID_PATTERN
from .. import ledger
from ..native_context import canonical
from ..native_protocol import MAX_BYTES, NativeProtocolError, load
from ..store import Refused, utcnow
from .validation import LedgerValidationError, VALIDATION_CODES, VALIDATION_PATHS


SCHEMA = "onlyfans.ledger-diagnostic.v1"
ROW_FIELDS = (
    "id", "ownerId", "user", "description", "status", "currency", "amount",
    "createdAt", "messageId", "clearedAt", "payoutPendingDays", "net", "fee", "vatAmount",
)
MAX_ISSUES = ledger.PAGE_SIZE


def _result(error_code=None):
    return dict(schema=SCHEMA, valid=False, applied=False, error_code=error_code,
                structure=None, issues=[], issues_truncated=False)


def _type(value):
    return {type(None): "null", bool: "boolean", int: "integer", Decimal: "decimal",
            str: "string", list: "array", dict: "object"}.get(type(value), "unsupported")


def _fields(value, names):
    """Names are local constants, never names supplied by the provider."""
    return {key: {"present": key in value, "type": _type(value[key]) if key in value else None}
            for key in names}


def _row_structure(row, index):
    result = {"row_index": index, "type": _type(row)}
    if type(row) is dict:
        result["fields"] = _fields(row, ROW_FIELDS)
        result["unknown_field_count"] = sum(key not in ROW_FIELDS for key in row)
        if type(row.get("user")) is dict:
            result["user_fields"] = _fields(row["user"], ("id",))
            result["user_unknown_field_count"] = sum(key != "id" for key in row["user"])
    return result


def _issue(exc, row_index=None):
    # Defense in depth: exception messages and unregistered attributes must
    # never become output, even if another parser starts raising them later.
    code = exc.code if isinstance(exc, LedgerValidationError) and exc.code in VALIDATION_CODES else "validation_failed"
    path = exc.path if isinstance(exc, LedgerValidationError) and exc.path in VALIDATION_PATHS else "$"
    index = exc.row_index if isinstance(exc, LedgerValidationError) and row_index is None else row_index
    if type(index) is not int or not 0 <= index < ledger.PAGE_SIZE:
        index = None
    return dict(code=code, path=path, row_index=index)


def _json(value):
    """Re-encode strict JSON with exact decimal tokens for single-row parsing."""
    if type(value) is Decimal:
        return str(value)
    if type(value) is list:
        return "[" + ",".join(_json(item) for item in value) + "]"
    if type(value) is dict:
        return "{" + ",".join(json.dumps(key, ensure_ascii=True) + ":" + _json(item)
                               for key, item in value.items()) + "}"
    return json.dumps(value, ensure_ascii=True, allow_nan=False)


def inspect_page(raw, *, owner_id, start, end):
    """Return structure and the first actual validation issue for every row.

    Uses the production parser for the complete page and each bounded row;
    complete-page checks include duplicate transaction identities. At most 20
    rows and 20 issues are emitted. Unknown key names and all response values
    are excluded. Loading uses the unchanged 65,536-byte production budget.
    """
    result = _result()
    try:
        ledger.parse_page(raw, owner_id=owner_id, start=start, end=end)
    except (NativeProtocolError, Refused, ValueError, TypeError, OverflowError, RecursionError) as exc:
        issue = _issue(exc)
        result["error_code"] = issue["code"]
        result["issues"].append(issue)
    else:
        result["valid"] = True
    try:
        value = load(raw)
    except (NativeProtocolError, ValueError, TypeError, OverflowError, RecursionError):
        # No second permissive loader: malformed/oversized input has no shape.
        return result
    rows = value.get("list")
    result["structure"] = dict(type="object", fields=_fields(value, ("list", "hasMore")),
        unknown_field_count=sum(key not in ("list", "hasMore") for key in value), rows=[])
    if type(rows) is not list:
        return result
    result["structure"].update(row_count=len(rows), rows_truncated=len(rows) > ledger.PAGE_SIZE)
    result["structure"]["rows"] = [_row_structure(row, index)
                                     for index, row in enumerate(rows[:ledger.PAGE_SIZE])]
    # Invalid page framing is diagnosed before any per-row interpretation.
    if (len(rows) > ledger.PAGE_SIZE or type(value.get("hasMore")) is not bool
            or value["hasMore"] and not rows):
        return result
    if result["valid"]:
        return result
    for index, row in enumerate(rows):
        try:
            # Unknown fields are ignored by the actual parser and must not
            # cause re-encoding overhead or encoding failures here.
            subset = {key: row[key] for key in ROW_FIELDS if key in row} if type(row) is dict else row
            if type(subset) is dict and type(subset.get("user")) is dict:
                subset["user"] = {"id": subset["user"]["id"]} if "id" in subset["user"] else {}
            encoded = _json({"list": [subset], "hasMore": False}).encode("utf-8")
            if len(encoded) > MAX_BYTES:
                # Re-encoding cannot enlarge the parser's accepted byte budget.
                raise NativeProtocolError("bounded row unavailable")
            ledger.parse_page(encoded, owner_id=owner_id, start=start, end=end)
        except (NativeProtocolError, Refused, ValueError, TypeError, OverflowError, UnicodeError, RecursionError) as exc:
            issue = _issue(exc, index)
            if issue not in result["issues"]:
                if len(result["issues"]) < MAX_ISSUES:
                    result["issues"].append(issue)
                else:
                    result["issues_truncated"] = True
    return result


class _DiagnosticRefused(Exception):
    """Codes are assigned only by this module, never by provider exceptions."""


def _scope(store, session, tenant, account, expected_offset):
    runtime = store._runtime(session, tenant, account)
    connection = session.get(OfProviderConnection, account)
    scan = session.get(OfLedgerScan, account)
    now = utcnow()
    if not ledger._current(runtime, connection, scan, now):
        raise _DiagnosticRefused("connection_unavailable")
    if scan is None:
        raise _DiagnosticRefused("held_scan_required")
    if scan.status == "running" or scan.lease_token is not None or scan.lease_until is not None:
        raise _DiagnosticRefused("scan_busy")
    if scan.status != "held" or scan.last_error != "unsupported_ledger_response":
        raise _DiagnosticRefused("held_scan_required")
    if scan.page_offset != expected_offset:
        raise _DiagnosticRefused("scan_offset_changed")
    if (type(scan.window_start) is not datetime or type(scan.window_end) is not datetime
            or scan.window_start.tzinfo is not None or scan.window_end.tzinfo is not None
            or not timedelta(0) <= scan.window_end - scan.window_start <= timedelta(days=90)
            or scan.window_end > now):
        raise _DiagnosticRefused("scan_window_invalid")
    # Compare metadata internally; the hash and all scope values stay private.
    # Include every persisted scan column so a transient cursor/lease change
    # recorded in updated_at invalidates this read even if offset returns to 40.
    snapshot = dict(runtime={"account_id": runtime.account_id, "binding_id": runtime.binding_id,
                             "configuration_revision": runtime.configuration_revision,
                             "enabled": runtime.enabled},
        connection={key: getattr(connection, key) for key in (
            "account_id", "binding_id", "owner_id", "revision", "status", "verified_at",
            "expires_at", "updated_at", "sealed_config")},
        scan={column.name: getattr(scan, column.name) for column in OfLedgerScan.__table__.columns})
    fingerprint = hashlib.sha256(canonical(snapshot).encode()).digest()
    return connection, scan, fingerprint


def collect_diagnostic(store, tenant, account, *, expected_offset=40):
    """Read exactly one held page through the existing scoped connection.

    Callable only in worker context. The native transaction read performs its
    existing identity GET followed by one transaction GET, with no retries and
    no widened timeout or size budget. A fresh post-read scope check must match
    before any structure is returned. All failures are redacted static codes.
    """
    result = _result()
    result["collection"] = dict(attempted=False, snapshot_unchanged=False)
    if (os.environ.get("ONLYFANS_PROCESS_ROLE") != "worker"
            or type(tenant) is not str or tenant == "deployment" or not ID_PATTERN.fullmatch(tenant)
            or type(account) is not str or not ID_PATTERN.fullmatch(account)
            or type(expected_offset) is not int or not 0 <= expected_offset <= ledger.MAX_OFFSET):
        result["error_code"] = "invalid_diagnostic_scope"
        return result
    try:
        with Session(store.engine) as session:
            connection, scan, before = _scope(store, session, tenant, account, expected_offset)
            from ..connections import _open
            clear = _open(connection, tenant)
            owner, expiry = connection.owner_id, connection.expires_at
            start, end = scan.window_start, scan.window_end
    except _DiagnosticRefused as exc:
        result["error_code"] = exc.args[0]
        return result
    except Exception:
        result["error_code"] = "diagnostic_scope_unavailable"
        return result
    raw, read_failed = None, False
    try:
        from ..provider_connector import NativeProviderConnector, session_from_capture
        transport = NativeProviderConnector(session_from_capture(account_id=account, owner_id=owner,
            policy_revision=1, capture=clear["capture"], x_hash=clear["xHash"], proxy_url=clear["proxyUrl"],
            expires_at=expiry.replace(tzinfo=timezone.utc)))
        result["collection"]["attempted"] = True
        raw = transport.transaction_page(offset=expected_offset, limit=ledger.PAGE_SIZE, start=start, end=end)
    except Exception:
        read_failed = True
    finally:
        clear = None
    try:
        with Session(store.engine) as session:
            _, _, after = _scope(store, session, tenant, account, expected_offset)
        if after != before:
            raise _DiagnosticRefused("diagnostic_state_changed")
    except Exception:
        result["error_code"] = "diagnostic_state_changed"
        return result
    result["collection"]["snapshot_unchanged"] = True
    if read_failed:
        result["error_code"] = "diagnostic_read_failed"
        return result
    inspected = inspect_page(raw, owner_id=owner, start=start, end=end)
    inspected["collection"] = result["collection"]
    return inspected
