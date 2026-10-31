"""Contextual-origin metadata and guards around FASTT's existing sender.

No platform client or scheduler lives here. Reservations survive crashes and
price/caption edits. Only positive ledger evidence releases an ignored offer.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from hashlib import sha256
import json
from types import SimpleNamespace

from twin_bridge import store
from onlyfans_runtime.ledger import RECENT_COVERAGE_FRESHNESS
from .ledger import Refused
from onlyfans_runtime.contextual_mode import KEY, mode

# Shared with the recent-ledger scheduler. This is the same bounded freshness
# contract, not a longer threshold used to hide stale provider coverage.
PURCHASE_FRESHNESS = RECENT_COVERAGE_FRESHNESS


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return sha256(canonical(value).encode()).hexdigest()


def stamp(value):
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def _scan_field(scan, key):
    try:
        return scan[key]
    except (KeyError, IndexError, TypeError):
        return getattr(scan, key, None)


def purchase_fresh(scan, connection, now):
    from onlyfans_runtime.ledger import coverage_proven
    if not scan or not connection:
        return False
    if any(_scan_field(scan, k) != connection[k] for k in ("binding_id", "connection_revision", "owner_id")):
        return False
    completed = stamp(_scan_field(scan, "last_complete_at"))
    window_end = stamp(_scan_field(scan, "window_end"))
    receipt = {**dict(scan), "last_complete_at":completed,
               "last_full_at":stamp(_scan_field(scan, "last_full_at"))}
    last_error = _scan_field(scan, "last_error") or ""
    error_allowed = (last_error in (None, "", "provider_read_deferred")
                     or last_error.startswith("provider_read_deferred:"))
    return (_scan_field(scan, "status") != "held" and error_allowed
            and coverage_proven(SimpleNamespace(**receipt))
            and completed is not None and window_end is not None
            and timedelta(0) <= now - completed <= PURCHASE_FRESHNESS
            and timedelta(0) <= now - window_end <= PURCHASE_FRESHNESS)


def purchase_freshness_detail(scan, connection, now):
    """Diagnostic only; purchase_fresh remains the delivery authority."""
    from onlyfans_runtime.ledger import coverage_proven
    if not scan or not connection:
        return {"cause": "missing-coverage"}
    if any(scan[k] != connection[k] for k in ("binding_id", "connection_revision", "owner_id")):
        return {"cause": "connection-mismatch"}
    reason_value = _scan_field(scan, "last_error")
    if reason_value:
        reason = str(reason_value)
        if reason.startswith("provider_read_deferred"):
            detail = {"cause": "provider-deferred",
                      "defer_reason": reason.split(":", 1)[1] if ":" in reason else "deferred"}
            for key in ("next_poll_at", "window_end", "last_complete_at"):
                value = stamp(_scan_field(scan, key))
                if value is not None:
                    detail[key] = value.isoformat()
            return detail
        return {"cause": "provider-backoff" if "rate" in reason else "provider-error",
                "provider_reason": reason}
    if _scan_field(scan, "status") == "held":
        return {"cause": "provider-held"}
    completed, cutoff = stamp(_scan_field(scan, "last_complete_at")), stamp(_scan_field(scan, "window_end"))
    receipt = {**dict(scan), "last_complete_at": completed,
               "last_full_at": stamp(_scan_field(scan, "last_full_at"))}
    if not coverage_proven(SimpleNamespace(**receipt)) or completed is None or cutoff is None:
        return {"cause": "missing-coverage"}
    return {"cause": "coverage-age", "coverage_age_seconds": (now-cutoff).total_seconds(),
            "completed_age_seconds": (now-completed).total_seconds()}


def confirmed_message(con, account, fan, origin):
    if origin["state"] == "sent" and origin["message_id"]:
        return origin["message_id"]
    if not store.has_table(con,"native_pilot_receipts"):
        return None
    receipt=con.execute("SELECT r.provider_message_id,r.evidence_json FROM native_pilot_receipts r "
        "JOIN native_pilots p ON p.pilot_id=r.pilot_id WHERE r.attempt_id=? AND r.state='sent' "
        "AND p.account_id=? AND p.fan_id=?",(origin["attempt_id"],account,fan)).fetchone()
    if receipt:
        try:
            retained=json.loads(receipt["evidence_json"])["dispatch_intent"]["content"]["contextual"]
            if canonical(retained)==origin["snapshot_json"]:
                return int(receipt["provider_message_id"])
        except (ValueError,TypeError,KeyError):
            pass
    return None


def ignored_contextual(con, account, fan):
    """Read only; expiry/ordinary chatter never clears an ignored contextual card."""
    rows = con.execute("SELECT * FROM of_contextual_offer WHERE account_id=? AND fan_id=? "
                       "AND state IN ('reserved','sent','unknown')", (account, fan)).fetchall()
    for row in rows:
        message_id=confirmed_message(con,account,fan,row)
        if message_id:
            paid = con.execute("SELECT 1 FROM content_offers o JOIN messages m ON m.account_id=o.account_id "
                "AND m.fan_id=o.fan_id AND m.message_id=o.offer_message_id WHERE o.account_id=? AND o.fan_id=? "
                "AND o.offer_message_id=? AND o.status='delivered' AND o.resolved_by='ppv_ledger' "
                "AND m.is_paid=TRUE", (account, fan, message_id)).fetchone()
            if paid:
                continue
        return True
    return False


def refuse_repeat_media(con, account, fan, terms):
    """Canonical media identity wins over changed prices, captions or workers.

    Applies only when the new origin table is installed, preserving old fixture
    stores. Explicit requests for different items remain eligible.
    """
    if not store.has_table(con, "of_contextual_offer"):
        return
    paid_media = set(terms["media_ids"]) - set(terms["preview_ids"])
    if not paid_media:
        return
    origins=con.execute("SELECT state,snapshot_json FROM of_contextual_offer WHERE account_id=? AND fan_id=? "
                        "AND state <> 'cancelled'",(account,fan)).fetchall()
    for origin in origins:
        snapshot=json.loads(origin["snapshot_json"])
        offered=set(snapshot["media_ids"])-set(snapshot["preview_ids"])
        if paid_media & offered:
            raise Refused("item-already-purchased" if origin["state"]=="purchased" else "item-already-pending")
    marks = ",".join("?" for _ in paid_media)
    row = con.execute("SELECT message_id,was_purchased FROM vault_sends WHERE account_id=? AND fan_id=? "
                      f"AND price_cents>0 AND media_id IN ({marks}) ORDER BY sent_at DESC LIMIT 1",
                      (account, fan, *sorted(paid_media))).fetchone()
    if row:
        raise Refused("item-already-purchased" if row["was_purchased"] else "item-already-pending")


def vault_revision(con, account, ids):
    fields = ("media_id", "kind", "removed_at", "content_hash", "updated_at_of", "description",
              "video_description", "ai_fields_json", "operator_overrides_json", "locked_fields_json")
    marks = ",".join("?" for _ in ids)
    rows = con.execute("SELECT " + ",".join(fields) + " FROM vault_items WHERE account_id=? "
                       f"AND media_id IN ({marks}) ORDER BY media_id", (account,*sorted(ids))).fetchall()
    if len(rows) != len(ids) or any(r["removed_at"] is not None for r in rows):
        raise Refused("contextual-content-unavailable")
    return digest([list(r) for r in rows])


def reconcile_origin(con, intent, attempt_id, provider_message_id):
    """Called only inside the existing positive-receipt reconciliation transaction."""
    metadata = (intent.get("content") or {}).get("contextual")
    if metadata is None:
        return
    row = con.execute("SELECT snapshot_json,attempt_id,state,message_id FROM of_contextual_offer "
                      "WHERE account_id=? AND decision_key=?", (intent["account_id"],metadata["decision_key"])).fetchone()
    if (not row or row["snapshot_json"] != canonical(metadata) or row["attempt_id"] != attempt_id
            or row["message_id"] is not None and row["message_id"] != provider_message_id
            or row["state"] not in ("reserved","unknown","sent","purchased")):
        raise Refused("contextual-recovery-conflict")
    if row["state"] != "purchased":
        con.execute("UPDATE of_contextual_offer SET state='sent',message_id=? WHERE account_id=? AND decision_key=?",
                    (provider_message_id,intent["account_id"],metadata["decision_key"]))


class ContextualAuthority:
    def __init__(self, path, account, fan, source_message_id, *, emit=lambda event: None, hold_check=None):
        self.path, self.account, self.fan = path, account, int(fan)
        self.source_message_id = int(source_message_id)
        self.decision_key = digest([account, self.fan, self.source_message_id])
        self.hold_check = hold_check
        self.emit = emit
        self.armed = None

    def _permission(self, con, now, required=None):
        raw = con.execute("SELECT ai_chatter_config_json FROM account_ai_config WHERE account_id=?",
                          (self.account,)).fetchone()
        from onlyfans_runtime.configuration import object_value
        settings = object_value(raw[0]) if raw else {}
        current = mode(settings)
        if current == "off" or required is not None and current != required:
            raise Refused("contextual-mode-changed")
        from onlyfans_runtime.configuration import SALES_FLAGS
        if not all(settings.get(k) is True for k in SALES_FLAGS):
            raise Refused("contextual-sales-disabled")
        runtime = con.execute("SELECT binding_id,configuration_revision,enabled FROM of_runtime WHERE account_id=?",
                              (self.account,)).fetchone()
        connection = con.execute("SELECT binding_id,revision AS connection_revision,owner_id,status,expires_at,last_error "
                                 "FROM of_provider_connection WHERE account_id=?", (self.account,)).fetchone()
        if (not runtime or not runtime["enabled"] or not connection or connection["status"] != "verified"
                or connection["last_error"] or stamp(connection["expires_at"]) <= now
                or connection["binding_id"] != runtime["binding_id"]):
            raise Refused("contextual-account-ineligible")
        from .postsale import unlinked_paid_fans
        if self.fan in unlinked_paid_fans(con, self.account):
            raise Refused("contextual-purchase-unlinked")
        scan = con.execute("SELECT * FROM of_ledger_scan WHERE account_id=?", (self.account,)).fetchone()
        if not purchase_fresh(scan, connection, now):
            self.emit({"event": "purchase-freshness-held", "account_id": self.account,
                       "fan_id": self.fan, **purchase_freshness_detail(scan, connection, now)})
            raise Refused("contextual-purchase-state-stale")
        return current, runtime

    def check(self, required=None):
        from .runner import now
        try:
            if self.hold_check is not None and self.hold_check():
                raise Refused("contextual-fan-ineligible")
            with store.readonly(self.path) as con:
                current, runtime = self._permission(con, now(), required)
                if ignored_contextual(con, self.account, self.fan):
                    raise Refused("contextual-offer-unresolved")
                if con.execute("SELECT 1 FROM of_contextual_offer WHERE account_id=? AND decision_key=?",
                               (self.account, self.decision_key)).fetchone():
                    raise Refused("contextual-turn-already-decided")
                return {"mode":current,"revision":runtime["configuration_revision"],"binding":runtime["binding_id"]}
        except Refused:
            raise
        except Exception:
            raise Refused("contextual-state-unavailable") from None

    @contextmanager
    def arm(self, delivery, permission, reason, supporting_ids):
        p = delivery.plan
        if p.account_id != self.account or p.fan_id != self.fan or supporting_ids != (self.source_message_id,):
            raise Refused("contextual-decision-scope")
        metadata = {"decision_key":self.decision_key,"source_message_id":self.source_message_id,
            "binding":permission["binding"],"revision":permission["revision"],
            "media_ids":list(p.attached),"preview_ids":list(p.previews),"price_cents":p.price_cents,
            "content_key":digest([self.account,sorted(p.media)]),"reason":reason[:120]}
        with store.readonly(self.path) as con:
            metadata["vault_revision"] = vault_revision(con,self.account,p.attached)
        self.armed = metadata
        try:
            yield
        finally:
            self.armed = None

    def reserve(self, terms, attempt_id):
        from .runner import now
        expected = self.armed
        if not expected or not terms or any(terms[k] != expected[k] for k in ("media_ids", "preview_ids", "price_cents")):
            raise Refused("contextual-snapshot-changed")
        with store.transaction(self.path) as con:
            _, runtime = self._permission(con, now(), "live")
            if (runtime["configuration_revision"],runtime["binding_id"]) != (expected["revision"],expected["binding"]):
                raise Refused("contextual-revision-changed")
            if vault_revision(con,self.account,expected["media_ids"]) != expected["vault_revision"]:
                raise Refused("contextual-content-changed")
            refuse_repeat_media(con,self.account,self.fan,terms)
            # Preserve old evidence, releasing the unique unresolved slot only on a
            # confirmed ledger match, atomically with the next reservation.
            for r in con.execute("SELECT * FROM of_contextual_offer WHERE account_id=? AND fan_id=? "
                                 "AND state IN ('reserved','sent','unknown')", (self.account,self.fan)).fetchall():
                if not ignored_contextual(con,self.account,self.fan):
                    con.execute("UPDATE of_contextual_offer SET state='purchased',message_id=? WHERE account_id=? AND decision_key=?",
                                (confirmed_message(con,self.account,self.fan,r),self.account,r["decision_key"]))
            if ignored_contextual(con,self.account,self.fan):
                raise Refused("contextual-offer-unresolved")
            if con.execute("SELECT 1 FROM of_contextual_offer WHERE account_id=? AND decision_key=?",
                           (self.account,self.decision_key)).fetchone():
                raise Refused("contextual-turn-already-decided")
            con.execute("INSERT INTO of_contextual_offer(account_id,decision_key,fan_id,binding_id,source_message_id,"
                "content_key,snapshot_json,attempt_id,state,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (self.account,self.decision_key,self.fan,expected["binding"],self.source_message_id,
                 expected["content_key"],canonical(expected),attempt_id,"reserved",store.iso(now())))
        return dict(expected)

    def before_post(self, metadata):
        from .runner import now
        with store.readonly(self.path) as con:
            _, runtime = self._permission(con, now(), "live")
            if (runtime["configuration_revision"],runtime["binding_id"]) != (metadata["revision"],metadata["binding"]):
                raise Refused("contextual-revision-changed")
            row = con.execute("SELECT snapshot_json,state FROM of_contextual_offer WHERE account_id=? AND decision_key=?",
                              (self.account,self.decision_key)).fetchone()
            if not row or row["state"] != "reserved" or row["snapshot_json"] != canonical(metadata):
                raise Refused("contextual-reservation-changed")
            if vault_revision(con,self.account,metadata["media_ids"]) != metadata["vault_revision"]:
                raise Refused("contextual-content-changed")

    def outcome(self, state, message_id=None):
        with store.transaction(self.path) as con:
            con.execute("UPDATE of_contextual_offer SET state=?,message_id=? WHERE account_id=? AND decision_key=? "
                        "AND state='reserved'", (state,message_id,self.account,self.decision_key))
