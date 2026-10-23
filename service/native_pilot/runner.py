"""Explicit single-chat native-engine experiment; importing starts nothing.

No server/scheduler is started. Only this subprocess receives the temporary
model/config/sender adapters. Live mode requires --enable-send; preflight is
read-only and never calls a model or sends a message.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .ledger import Ledger, Refused
from .scope import AccountScope


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def emit(event):
    print(json.dumps(event, sort_keys=True, allow_nan=False), flush=True)


@contextmanager
def readonly(path):
    con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only=ON")
        con.execute("BEGIN")
        yield con
    finally:
        con.rollback()
        con.close()


def check_local(path, account, fan):
    """Inspect only the chosen account's configuration and delivery metadata."""
    with readonly(path) as con:
        if not con.execute("SELECT 1 FROM accounts WHERE id=?", (account,)).fetchone():
            raise Refused("account-missing")
        if con.execute("SELECT 1 FROM automation_rules WHERE account_id=? AND is_enabled=1 LIMIT 1", (account,)).fetchone():
            raise Refused("account-rules-enabled")
        # gen_info only updates fan profiles; it cannot dispatch messages.
        # Keep all sending, unknown and unclassified jobs blocking admission.
        if con.execute("SELECT 1 FROM scheduled_jobs WHERE account_id=? AND status IN ('pending','running','queued') AND COALESCE(kind,'') != 'gen_info' LIMIT 1", (account,)).fetchone():
            raise Refused("account-jobs-open")
        row = con.execute("SELECT * FROM account_ai_config WHERE account_id=?", (account,)).fetchone()
        if row:
            for key in row.keys():
                if key.endswith("config_json") and row[key]:
                    value = json.loads(row[key])
                    if not isinstance(value, dict):
                        raise Refused("account-config-invalid")
                    if value.get("enabled"):
                        raise Refused("account-automation-enabled")
        # Suppressing the offer resolver is permitted only with no obligation.
        if con.execute("SELECT 1 FROM content_offers WHERE account_id=? AND fan_id=? AND status IN ('open','paid','delivering') LIMIT 1", (account, fan)).fetchone():
            raise Refused("chat-offer-open")
        if con.execute("SELECT 1 FROM pending_offer WHERE account_id=? AND fan_id=? LIMIT 1", (account, fan)).fetchone():
            raise Refused("chat-offer-open")
        if con.execute("SELECT 1 FROM twin_bridge_send_attempts WHERE account_id=? AND fan_id=? AND state IN ('dispatching','delivery_unknown') LIMIT 1", (account, fan)).fetchone():
            raise Refused("delivery-unresolved")
        if con.execute("SELECT 1 FROM twin_bridge_turns WHERE account_id=? AND state NOT IN ('delivered','cancelled','parked') LIMIT 1", (account,)).fetchone():
            raise Refused("twin-work-open")
        return {"creator_profile": "configured" if row and (row["persona"] or "").strip() else "native-default",
                "account_id": account, "fan_id": fan}


def read_sources(path, account, fan, ids):
    if not ids or any(type(mid) is not int or mid <= 0 for mid in ids) or len(set(ids)) != len(ids):
        raise Refused("source-ids-invalid")
    marks = ",".join("?" for _ in ids)
    with readonly(path) as con:
        rows = con.execute(f"SELECT message_id,body,created_at FROM messages WHERE account_id=? AND fan_id=? AND message_id IN ({marks})",
                           (account, fan, *ids)).fetchall()
    if {r["message_id"] for r in rows} != set(ids):
        raise Refused("source-missing")
    return {r["message_id"]: dict(r) for r in rows}


def check_provider_history(history, sources, confirmed_ids, *, account, fan):
    """Refuse changed inputs, new inputs, and unproved external outbound activity."""
    from twin_bridge.contract import parse_canonical_timestamp
    from twin_bridge.delivery import external_outbound_ids
    from twin_bridge.normalize import html_to_text

    if not sources:
        raise Refused("source-missing")
    # Provider timestamps are normalized to milliseconds by the shared parser.
    # Compare at that same precision, never discard a same-millisecond source.
    since = min(datetime.fromisoformat(r["created_at"]) for r in sources.values())
    since = since.replace(microsecond=since.microsecond // 1000 * 1000)
    if external_outbound_ids(history, set(confirmed_ids), creator_id=account, fan_id=fan, since=since):
        raise Refused("external-outbound")
    seen = set()
    for item in history:
        mid = item.get("id")
        if isinstance(mid, bool) or not isinstance(mid, (str, int)) or not str(mid).isascii() or not str(mid).isdigit():
            raise Refused("history-id-invalid")
        mid = int(mid)
        stamp = parse_canonical_timestamp(item.get("createdAt"), "createdAt")
        for field in ("withUser", "user"):
            if field in item:
                bound = item[field]
                if not isinstance(bound, dict) or isinstance(bound.get("id"), bool) or str(bound.get("id")) != str(fan):
                    raise Refused("history-recipient-mismatch")
        if stamp < since:
            continue
        if mid in confirmed_ids:
            continue
        if mid not in sources:
            raise Refused("new-message-during-generation")
        source_stamp = datetime.fromisoformat(sources[mid]["created_at"])
        source_stamp = source_stamp.replace(microsecond=source_stamp.microsecond // 1000 * 1000)
        if stamp != source_stamp:
            raise Refused("source-time-changed")
        if item.get("isUnsent"):
            raise Refused("source-invalidated")
        if (item.get("isTip") or item.get("lockedText") or item.get("isLocked") or item.get("isReaction") or
                item.get("media") or item.get("mediaFiles") or item.get("previews") or
                item.get("mediaCount") or item.get("gif") or item.get("giphyId")):
            raise Refused("source-not-plain-text")
        if isinstance(item.get("price"), bool) or item.get("price") not in (None, 0, 0.0, "0", "0.00"):
            raise Refused("source-not-free-text")
        if not isinstance(item.get("text"), str) or html_to_text(item["text"]) != html_to_text(sources[mid]["body"]):
            raise Refused("source-text-changed")
        seen.add(mid)
    if seen != set(sources):
        raise Refused("source-missing")


class Gateway:
    def __init__(self, ledger, pilot, client, loop, content=None, scope=None):
        self.ledger, self.pilot, self.client, self.loop = ledger, pilot, client, loop
        self.content = content
        # pilot.account_id is the Twin/DB-side id; everything said to or read from
        # OnlyFans (history, receipts, client identity) uses the provider-side id.
        self.scope = scope or AccountScope.parse(pilot.account_id)
        from twin_bridge.platform import OnlyFansTextAdapter
        self.reader = OnlyFansTextAdapter(self.scope.provider_id, client=client)
        self.input_key = None
        self.sources = {}
        self.bubble = 0
        self.stopped_reason = None

    def send(self, fan, text, kwargs):
        if type(fan) is not int or fan != self.pilot.fan_id or (kwargs and not self.content) or not isinstance(text, str) or not text.strip():
            self.stopped_reason = "send-scope-refused"
            raise Refused(self.stopped_reason)
        terms = self.content.terms(kwargs) if self.content else None
        return asyncio.run_coroutine_threadsafe(self._send(text, terms), self.loop).result()

    def stopped(self):
        if self.stopped_reason:
            return True
        state = self.ledger.status(self.pilot.pilot_id)
        if state["state"] != "active":
            self.stopped_reason = state["reason"] or "pilot-stopped"
        elif sum(state["attempts"].values()) >= self.pilot.end_limit - self.pilot.baseline_used:
            self.stopped_reason = "send-limit-reached"
        elif now() >= datetime.fromisoformat(self.pilot.deadline):
            self.stopped_reason = "time-limit-reached"
        return bool(self.stopped_reason)

    async def _send(self, text, terms=None):
        from twin_bridge.coordination import authorize_dispatch, revoke_dispatch, guarded_send
        from twin_bridge.platform import escape_plain_text, validate_created_message
        from twin_bridge.normalize import html_to_text

        attempt = None
        settled = False
        try:
            if self.stopped() or not self.input_key:
                raise Refused(self.stopped_reason or "pilot-stopped")
            if len(text.encode("utf-16-le")) // 2 > 4000:
                raise Refused("output-too-long")
            since = min(datetime.fromisoformat(r["created_at"]) for r in self.sources.values())
            history = await self.reader.preflight_history(self.pilot.fan_id, since=since, source_ids=set(self.sources))
            receipts = self.ledger.receipts(pilot_id=self.pilot.pilot_id, input_key=self.input_key)
            known = {int(r["provider_message_id"]) for r in receipts if r["state"] == "sent"}
            check_provider_history(history, self.sources, known, account=self.scope.provider_id, fan=self.pilot.fan_id)
            check_local(self.ledger.path, self.pilot.account_id, self.pilot.fan_id)
            if self.content:
                with readonly(self.ledger.path) as con:
                    self.content.check_catalog(con, self.pilot.account_id)
                    if terms:
                        row = con.execute("SELECT enabled FROM catalog_items WHERE id=? AND account_id=?", (terms["catalog_id"], self.pilot.account_id)).fetchone()
                        if not row or not row["enabled"]:
                            raise Refused("content-item-disabled")
                if terms:
                    for media_id in terms["media_ids"]:
                        asset = await asyncio.to_thread(self.client.vault_media_by_id, media_id)
                        if (str(asset.get("id")) != str(media_id) or asset.get("type") != terms["media_types"][str(media_id)]
                                or asset.get("canView") is not True):
                            raise Refused("content-asset-unavailable")
            self.bubble += 1
            attempt = self.ledger.begin_attempt(pilot_id=self.pilot.pilot_id, owner_token=self.pilot.owner_token,
                input_key=self.input_key, bubble_no=self.bubble, revision=self.pilot.revision, now=now())
            token = authorize_dispatch(self.pilot.account_id, self.pilot.fan_id, turn_id=attempt.turn_id,
                attempt_no=attempt.attempt_no, owner_token=self.pilot.owner_token, revision=self.pilot.revision)
            if terms:
                # Retain the frozen media/price intent even if the POST outcome
                # is unknown. No new table and no automatic delivery recovery.
                with sqlite3.connect(self.ledger.path) as con:
                    con.execute("UPDATE native_pilot_receipts SET evidence_json=? WHERE attempt_id=? AND state='dispatching'",
                                (json.dumps({"content_intent": terms}, sort_keys=True), attempt.id))

            def physical_post():
                # Re-read stop/owner after the thread handoff, immediately at the wire.
                with readonly(self.ledger.path) as con:
                    row = con.execute("SELECT state,owner_token,revision,owner_expires_at,reserved FROM twin_bridge_conversations WHERE account_id=? AND fan_id=?",
                                      (self.pilot.account_id, self.pilot.fan_id)).fetchone()
                    p = con.execute("SELECT state FROM native_pilots WHERE pilot_id=?", (self.pilot.pilot_id,)).fetchone()
                    if (not row or row["state"] != "active" or not row["reserved"] or row["owner_token"] != self.pilot.owner_token or
                            row["revision"] != self.pilot.revision or not p or p["state"] != "active" or
                            not row["owner_expires_at"] or now() >= datetime.fromisoformat(row["owner_expires_at"]) or
                            now() >= datetime.fromisoformat(self.pilot.deadline)):
                        raise Refused("authority-lost-before-post")
                self.ledger.validate_dispatch(pilot_id=self.pilot.pilot_id,
                    owner_token=self.pilot.owner_token, attempt_id=attempt.id, now=now())
                with guarded_send(self.pilot.account_id, self.pilot.fan_id, purpose="twin_bridge", dispatch_token=token):
                    if terms:
                        from of_client import API_BASE
                        body = {"text": escape_plain_text(text), "lockedText": False,
                                "mediaFiles": list(terms["media_ids"]), "price": terms["price_cents"] / 100,
                                "previews": list(terms["preview_ids"]), "rfTag": [], "rfGuest": [], "rfPartner": [],
                                "isForward": False}
                        return self.client.post_json(f"{API_BASE}/chats/{self.pilot.fan_id}/messages", json_body=body, proxy_tries=1,
                                                    allow_redirects=False)
                    return self.client.send_text_once_body(self.pilot.fan_id, escape_plain_text(text))
            try:
                raw = await asyncio.to_thread(physical_post)
            finally:
                revoke_dispatch(self.pilot.account_id, self.pilot.fan_id, token)
            validated = validate_created_message(raw, chat_id=self.pilot.fan_id, creator_id=self.scope.provider_id)
            if terms:
                content_evidence = self.content.evidence(raw, terms)
                if not isinstance(raw.get("text"), str) or html_to_text(raw["text"]) != html_to_text(escape_plain_text(text)):
                    raise Refused("content-receipt-text")
            elif (not isinstance(raw.get("text"), str) or html_to_text(raw["text"]) != html_to_text(escape_plain_text(text)) or
                    any(raw.get(k) for k in ("media", "mediaFiles", "mediaCount", "previews", "lockedText", "isTip", "isUnsent")) or
                    isinstance(raw.get("price"), bool) or raw.get("price") not in (None, 0, 0.0, "0", "0.00")):
                raise Refused("provider-response-contradicts-text-send")
            self.ledger.record_sent(pilot_id=self.pilot.pilot_id, owner_token=self.pilot.owner_token,
                attempt_id=attempt.id, provider_message_id=validated.provider_message_id,
                validated_response=raw, evidence={**validated.evidence, **({"content": content_evidence} if terms else {})}, now=now())
            settled = True
            emit({"event": "sent", "engine": "fastt-native", "provider_message_id": validated.provider_message_id,
                  "pilot_sent": len([r for r in self.ledger.receipts(pilot_id=self.pilot.pilot_id) if r["state"] == "sent"]),
                  "limit": self.pilot.end_limit - self.pilot.baseline_used})
            return raw
        except Exception as exc:
            self.stopped_reason = exc.reason if isinstance(exc, Refused) else "send-check-failed"
            if attempt is not None and not settled:
                self.ledger.record_unknown(pilot_id=self.pilot.pilot_id, owner_token=self.pilot.owner_token,
                                           attempt_id=attempt.id, now=now())
                self.stopped_reason = "delivery-unknown"
            raise Refused(self.stopped_reason) from None


CLEAN_STOPS = frozenset({"preflight-only", "time-limit-reached", "send-limit-reached", "operator-stop",
                         "native-declined-turn", "paid-card-sent-test-complete"})


def db_account_id(account):
    """The ONE call site where the pilot/FASTT account id becomes the canonical DB id."""
    return AccountScope.parse(account).db_id


async def report_to_twin(ledger, pilot, run_error=None):
    """Accepted sends, unknowns and errors into Twin's reporting tables (see reporting.py).

    Never raises: reporting must not change the pilot's stop/ownership decisions."""
    try:
        from .reporting import report_outcomes
        result = await report_outcomes(account_id=db_account_id(pilot.account_id), fan_id=pilot.fan_id,
                                       receipts=ledger.receipts(pilot_id=pilot.pilot_id), run_error=run_error)
        emit({"event": "twin-report", **result})
    except Exception:
        emit({"event": "twin-report-failed"})


def begin_native_run(path, account):
    """Create the native audit row without invoking the automation scheduler."""
    with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=rw", uri=True) as con:
        cursor = con.execute("INSERT INTO automation_runs(account_id,kind,started_at,status) VALUES(?,'ai_chatter',?,'running')",
                             (account, now().isoformat(sep=" ")))
        return int(cursor.lastrowid)


def finish_native_run(path, run_id, stats, reason=None):
    # Only aggregate counters/fixed reasons, never native prompt/error text.
    counters = {k: v for k, v in (stats or {}).items() if isinstance(k, str) and type(v) in (int, bool)}
    with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=rw", uri=True) as con:
        con.execute("UPDATE automation_runs SET completed_at=?,status=?,stats_json=?,error_text=? WHERE id=?",
                    (now().isoformat(sep=" "), "error" if reason else "done", json.dumps(counters), reason, run_id))


NO_EMPLOYEE_CHECK = object()


def verify_attribution(path, account, fan, receipts, content=None, automation_employee_id=NO_EMPLOYEE_CHECK):
    """Every accepted send must be a stored AI row; with an employee id given it must
    carry exactly the Automation sentinel (never NULL, never a human's id)."""
    with readonly(path) as con:
        for receipt in receipts:
            if receipt["state"] != "sent":
                raise Refused("delivery-unresolved")
            row = con.execute("SELECT direction,automation_kind,price_cents,media_count"
                              + ("" if automation_employee_id is NO_EMPLOYEE_CHECK else ",sent_by_employee_id")
                              + " FROM messages WHERE account_id=? AND fan_id=? AND message_id=?",
                              (account, fan, int(receipt["provider_message_id"]))).fetchone()
            stored = con.execute("SELECT response_json FROM native_pilot_receipts WHERE attempt_id=?", (receipt["attempt_id"],)).fetchone() if content else None
            raw = json.loads(stored[0]) if stored else {}
            cents = int(round(float(raw.get("price") or 0) * 100)) if content else 0
            kinds = ("ai_chatter", "ai_upseller") if content else ("ai_chatter",)
            if not row or row["direction"] != "out" or row["automation_kind"] not in kinds or row["price_cents"] != cents or (not content and row["media_count"] != 0):
                raise Refused("native-attribution-missing")
            if automation_employee_id is not NO_EMPLOYEE_CHECK and row["sent_by_employee_id"] != automation_employee_id:
                raise Refused("native-attribution-employee")


async def run(args):
    from db.engine import engine
    from twin_bridge.coordination import sqlite_path, reserve_ownership, clear_owner
    from .attribution import read_sentinels, require_sentinels
    from .budget import DailyBudget
    from .connection import build_client, load_verified_connection
    from .engine import NativeEngine
    from .model import NativePilotModel, MODEL, ENDPOINT

    # The one account-id boundary: `db` for Twin's tables/FASTT's queries/the ledger,
    # `scope.provider_id` for OnlyFans. Live data keeps its `of-<owner>` ids.
    scope = AccountScope.parse(args.account)
    db = scope.db_id
    path = sqlite_path()
    if not path:
        raise Refused("sqlite-required-for-pilot")
    profile = check_local(path, db, args.fan)
    from .content import ContentPlan
    content = ContentPlan(args.content_manifest, db, args.fan) if getattr(args, "content_manifest", None) else None
    if content:
        with readonly(path) as con:
            content.check_catalog(con, db)
    client = None
    pilot = None
    gateway = None
    model = None
    prior_catalog = None
    ledger = Ledger(path)
    reason = "preflight-only"
    try:
        # Current verified session + proxy + account-scoped model credential,
        # read-only from Twin's encrypted connection record. Fails closed.
        connection = await load_verified_connection(scope)
        client = await asyncio.to_thread(build_client, connection)
        me = await asyncio.to_thread(client.me)
        if str(me.get("id")) != scope.provider_id or str(me.get("username", "")).lstrip("@").casefold() != args.handle.casefold():
            raise Refused("live-account-mismatch")
        found = await read_sentinels()
        emit({"event": "preflight-ok", **profile, "model": MODEL, "model_id": connection.model_id,
              "daily_cost_cap_cents": connection.daily_cost_cap_cents,
              "proxy": "configured" if connection.proxy_url else "none", "creator_handle": args.handle,
              "attribution": {"automation": found.automation is not None, "human": found.human is not None}})
        if not args.enable_send:
            return
        sentinels = await require_sentinels()
        start = now()
        pilot = ledger.activate(pilot_id=args.pilot_id, account_id=db, fan_id=args.fan,
            owner_token="native-" + secrets.token_hex(24), cutoff=start,
            deadline=start + timedelta(seconds=args.max_seconds), limit=args.send_limit, now=start)
        if not await reserve_ownership(account_id=db, fan_id=args.fan, owner_token=pilot.owner_token,
                                       now=now(), ttl_s=max(1, (datetime.fromisoformat(pilot.deadline) - now()).total_seconds())):
            raise Refused("ownership-unavailable")
        gateway = Gateway(ledger, pilot, client, asyncio.get_running_loop(), content=content, scope=scope)
        model = NativePilotModel(api_key=connection.model_api_key, account_id=db, fan_id=args.fan,
            deadline=time.monotonic() + args.max_seconds, should_stop=gateway.stopped, on_event=emit,
            reserve_call=lambda: ledger.reserve_model_call(pilot_id=pilot.pilot_id, owner_token=pilot.owner_token, now=now()),
            budget=DailyBudget(scope, connection.model_id, endpoint=ENDPOINT))
        if content:
            # The reviewed card must be sellable for this run. Remember the live
            # flag so it is restored, not forced, when the pilot ends.
            with sqlite3.connect(path) as con:
                prior_catalog = con.execute("SELECT enabled FROM catalog_items WHERE account_id=? AND id=?",
                                            (db, content.item["catalog_id"])).fetchone()[0]
                con.execute("UPDATE catalog_items SET enabled=1 WHERE account_id=? AND id=?", (db, content.item["catalog_id"]))
        reason = "time-limit-reached"
        context = None
        if getattr(args, "twin_context", False):
            from .twin_context import TwinPromptContext
            context = TwinPromptContext()
        with NativeEngine(db, args.fan, chat=model.chat, send=gateway.send, emit=emit, content=content,
                          context=context) as native:
            emit({"event": "ready", "pilot_id": pilot.pilot_id, "account_id": db, "fan_id": args.fan,
                  "engine": "fastt-native", "limit": args.send_limit, "deadline_utc": pilot.deadline,
                  "fresh_messages_only": True, "creator_profile": profile["creator_profile"]})
            while now() < datetime.fromisoformat(pilot.deadline):
                state = ledger.status(pilot.pilot_id)
                if state["state"] != "active":
                    reason = state["reason"] or "operator-stop"
                    break
                attempts = state["attempts"]
                if sum(attempts.values()) >= args.send_limit:
                    reason = "send-limit-reached"
                    break
                snap = ledger.snapshot(pilot_id=pilot.pilot_id, owner_token=pilot.owner_token, now=now())
                if snap is None:
                    await asyncio.sleep(2)
                    continue
                # Persisted receipt quiet window; no transaction is held while waiting.
                if (now() - datetime.fromisoformat(snap.ingested_at)).total_seconds() < 7:
                    await asyncio.sleep(2)
                    continue
                gateway.sources = read_sources(path, db, args.fan, getattr(snap, "member_ids", (snap.message_id,)))
                gateway.input_key = ledger.claim_input(pilot_id=pilot.pilot_id, owner_token=pilot.owner_token, snapshot=snap, now=now())
                gateway.bubble = 0
                emit({"event": "generating", "source_message_id": str(snap.message_id), "engine": "fastt-native"})
                run_id = begin_native_run(path, db)
                stats = None
                run_error = None
                try:
                    stats = await native.run(run_id=run_id)
                    if gateway.stopped_reason or model.stopped_reason:
                        raise Refused(gateway.stopped_reason or model.stopped_reason)
                    if not isinstance(stats, dict) or stats.get("errors") or stats.get("cap_hit"):
                        raise Refused("native-turn-failed")
                except BaseException as exc:
                    run_error = exc.reason if isinstance(exc, Refused) else "native-turn-failed"
                    raise
                finally:
                    finish_native_run(path, run_id, stats, run_error)
                receipts = ledger.receipts(pilot_id=pilot.pilot_id, input_key=gateway.input_key)
                verify_attribution(path, db, args.fan, receipts, content=content,
                                   automation_employee_id=sentinels.automation)
                await report_to_twin(ledger, pilot)
                ledger.finish_input(pilot_id=pilot.pilot_id, owner_token=pilot.owner_token, input_key=gateway.input_key, now=now())
                if content:
                    with readonly(path) as con:
                        rows = con.execute("SELECT evidence_json FROM native_pilot_receipts WHERE pilot_id=? AND state='sent'", (pilot.pilot_id,)).fetchall()
                        sent_media = [json.loads(r[0]).get("content") for r in rows]
                    if any(x and x["price_cents"] == 300 for x in sent_media):
                        reason = "paid-card-sent-test-complete"
                        break
                if not receipts:
                    # Native last-look can discard a draft when a newer inbound
                    # arrives during generation. Consume the old claim, then
                    # admit only a different fresh snapshot through all gates.
                    if (stats or {}).get("stale_drops") == {"fan_spoke_again": 1}:
                        newer = ledger.snapshot(pilot_id=pilot.pilot_id, owner_token=pilot.owner_token, now=now())
                        if newer is not None and newer.message_id != snap.message_id:
                            emit({"event": "new-input-pending", "reason": "fan-spoke-again"})
                            continue
                    emit({"event": "native-no-reply", "skip_counts": {k: v for k, v in (stats or {}).items() if k.startswith("skipped_") and isinstance(v, int) and v}})
                    reason = "native-declined-turn"
                    break
    except Exception as exc:
        reason = (gateway.stopped_reason if gateway else None) or (model.stopped_reason if model else None) or (
            exc.reason if isinstance(exc, Refused) else "pilot-error")
        emit({"event": "stopped", "reason": reason})
        raise Refused(reason) from None
    finally:
        try:
            if pilot is not None:
                await report_to_twin(ledger, pilot, None if reason in CLEAN_STOPS else reason)
                try:
                    ledger.stop(pilot_id=pilot.pilot_id, owner_token=pilot.owner_token, reason=reason, now=now())
                finally:
                    clear_owner(pilot.account_id, pilot.fan_id, pilot.owner_token)
                emit({"event": "finished", **ledger.status(pilot.pilot_id)})
                if content and prior_catalog is not None:
                    with sqlite3.connect(path) as con:
                        con.execute("UPDATE catalog_items SET enabled=? WHERE account_id=? AND id=?",
                                    (prior_catalog, db, content.item["catalog_id"]))
        finally:
            try:
                if client is not None:
                    client.http.close()
            finally:
                await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True, help="Twin account id (of-<owner>) or the bare owner id")
    parser.add_argument("--fan", type=int, required=True)
    parser.add_argument("--handle", required=True)
    parser.add_argument("--pilot-id", required=True)
    parser.add_argument("--send-limit", type=int, default=10)
    parser.add_argument("--max-seconds", type=int, default=1800)
    parser.add_argument("--enable-send", action="store_true")
    parser.add_argument("--twin-context", action="store_true",
                        help="Feed Twin's approved persona, creator facts and fan memory through the single prompt hook")
    parser.add_argument("--content-manifest", help="Explicit private single-card ($3) pilot manifest; text only when omitted")
    args = parser.parse_args()
    if not AccountScope.valid(args.account) or args.fan <= 0 or not 1 <= args.send_limit <= 30 or not 60 <= args.max_seconds <= 1800:
        parser.error("invalid pilot scope or bounds")
    # Application logs can contain provider errors/private context; pilot emits
    # its own fixed metadata only. This changes only this dedicated process.
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        emit({"event": "stopped", "reason": "operator-interrupt"})
        return 1
    except Exception as exc:
        emit({"event": "not-running", "reason": exc.reason if isinstance(exc, Refused) else "preflight-error"})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
