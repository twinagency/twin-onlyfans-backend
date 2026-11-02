"""Explicit single-chat native-engine experiment; importing starts nothing.

No server/scheduler is started. Only this subprocess receives the temporary
model/config/sender adapters. Live mode requires --enable-send; preflight is
read-only and never calls a model or sends a message.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import logging
import math
from email.utils import parsedate_to_datetime
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from twin_bridge import store

from .ledger import Ledger, Refused
from .scope import AccountScope


_DASH = re.compile(r"\s*[\u2014\u2013]\s*")


def no_dashes(text):
    """Owner rule: no em/en dashes in fan-visible text (chat or card caption)."""
    out = _DASH.sub(", ", text).strip(", ")
    return out or text


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def emit(event):
    print(json.dumps(event, sort_keys=True, allow_nan=False), flush=True)


# ``path`` throughout this module is the pilot store target: a SQLite file path
# or the PostgreSQL URL of the application database (see twin_bridge.store).
readonly = store.readonly


def check_local(path, account, fan, approved_item=None, open_offer_blocks=True):
    """Inspect only the chosen account's configuration and delivery metadata."""
    with readonly(path) as con:
        if not con.execute("SELECT 1 FROM accounts WHERE id=?", (account,)).fetchone():
            raise Refused("account-missing")
        if con.execute("SELECT 1 FROM automation_rules WHERE account_id=? AND is_enabled LIMIT 1", (account,)).fetchone():
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
        # An open PPV of the one approved item is settled by OnlyFans itself (the fan unlocks it).
        # approved_item: one catalogue id, or an iterable of listed ids.
        ids = sorted({approved_item} if isinstance(approved_item, int) else set(approved_item or ()))
        approved = (" AND NOT (mode='ppv' AND status='open' AND item_id IN (%s))" % ",".join("?" * len(ids)),
                    tuple(ids)) if ids else ("", ())
        # Engine v2: the persistent service passes open_offer_blocks=False. An unpaid (or paid, still being
        # delivered) offer is settled by OnlyFans / Twin's paid-media path, not by this runner; it must not stop
        # an ordinary text reply. The bounded pilot keeps the original refusal.
        if open_offer_blocks:
            if con.execute("SELECT 1 FROM content_offers WHERE account_id=? AND fan_id=? AND status IN ('open','paid','delivering')"
                           + approved[0] + " LIMIT 1", (account, fan, *approved[1])).fetchone():
                raise Refused("chat-offer-open")
            if con.execute("SELECT 1 FROM pending_offer WHERE account_id=? AND fan_id=? LIMIT 1", (account, fan)).fetchone():
                raise Refused("chat-offer-open")
        if con.execute("SELECT 1 FROM twin_bridge_send_attempts WHERE account_id=? AND fan_id=? AND state IN ('dispatching','delivery_unknown') LIMIT 1", (account, fan)).fetchone():
            raise Refused("delivery-unresolved")
        if con.execute("SELECT 1 FROM twin_bridge_turns WHERE account_id=? AND fan_id=? "
                       "AND state NOT IN ('delivered','cancelled','parked') LIMIT 1", (account, fan)).fetchone():
            raise Refused("twin-work-open")
        return {"creator_profile": "configured" if row and (row["persona"] or "").strip() else "native-default",
                "account_id": account, "fan_id": fan}


def require_store_schema(path, followups=False):
    """PostgreSQL: create the separate ``fastt_runtime`` schema's tables if absent, then verify them.

    The application schema (revision 0091) is untouched; only the nine pilot/bridge tables
    in ``fastt_runtime`` are added (idempotent, additive). Without the writer-marker table
    the transport boundary could not prove that no other local writer is in flight, so a
    table still missing afterwards is a refusal, never a silent fallback.
    """
    if not store.is_pg(path):
        return
    store.ensure_runtime_schema(path)
    with readonly(path) as con:
        for table in ("twin_bridge_conversations", "twin_bridge_send_attempts", "twin_bridge_turns",
                      "twin_bridge_writes", "native_pilots", "native_pilot_inputs", "native_pilot_receipts",
                      *(("native_pilot_followups",) if followups else ())):
            if not store.has_table(con, table):
                raise Refused("pilot-schema-missing")


def read_sources(path, account, fan, ids):
    if not ids or any(type(mid) is not int or mid <= 0 for mid in ids) or len(set(ids)) != len(ids):
        raise Refused("source-ids-invalid")
    marks = ",".join("?" for _ in ids)
    with readonly(path) as con:
        rows = con.execute(f"SELECT message_id,body,created_at FROM messages WHERE account_id=? AND fan_id=? AND message_id IN ({marks})",
                           (account, fan, *ids)).fetchall()
    if {r["message_id"] for r in rows} != set(ids):
        raise Refused("source-missing")
    sources = {r["message_id"]: dict(r) for r in rows}
    # A photo FASTT described (messages.image_desc) is a readable source whose provider frame carries
    # media; read separately so a store without the column (fixtures) simply has none.
    try:
        with readonly(path) as con:
            described = {r["message_id"] for r in con.execute(
                f"SELECT message_id FROM messages WHERE account_id=? AND fan_id=? AND message_id IN ({marks}) "
                "AND image_desc IS NOT NULL AND image_desc<>''", (account, fan, *ids)).fetchall()}
    except Exception:
        described = set()
    for mid in described:
        sources[mid]["described"] = True
    return sources


def check_provider_history(history, sources, confirmed_ids, *, account, fan, dashboard_handover=False, tolerate_newer=False):
    """Validate inputs; dashboard-managed chats use explicit control for handover."""
    from twin_bridge.contract import parse_canonical_timestamp
    from twin_bridge.delivery import external_outbound_ids
    from twin_bridge.normalize import html_to_text

    if not sources:
        raise Refused("source-missing")
    # Provider timestamps are normalized to milliseconds by the shared parser.
    # Compare at that same precision, never discard a same-millisecond source.
    since = min(datetime.fromisoformat(r["created_at"]) for r in sources.values())
    since = since.replace(microsecond=since.microsecond // 1000 * 1000)
    outbound = set(external_outbound_ids(history, set(confirmed_ids), creator_id=account, fan_id=fan, since=since))
    if outbound and not dashboard_handover:
        raise Refused("external-outbound")
    latest_source = max(datetime.fromisoformat(r["created_at"]) for r in sources.values())
    latest_source = latest_source.replace(microsecond=latest_source.microsecond // 1000 * 1000)
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
        if dashboard_handover and mid in outbound:
            if mid in sources:
                raise Refused("source-direction-changed")
            continue
        if mid in confirmed_ids:
            continue
        if mid not in sources:
            if tolerate_newer and not item.get("isFromMe") and stamp > latest_source:
                continue        # hard-maximum burst: a later fan message does not stale the reply (answered next)
            raise Refused("new-message-during-generation")
        source_stamp = datetime.fromisoformat(sources[mid]["created_at"])
        source_stamp = source_stamp.replace(microsecond=source_stamp.microsecond // 1000 * 1000)
        if stamp != source_stamp:
            raise Refused("source-time-changed")
        if item.get("isUnsent"):
            raise Refused("source-invalidated")
        # A described photo (FASTT's inbound_describe) may carry plain media; nothing else may.
        photo_ok = bool(sources[mid].get("described"))
        if (item.get("isTip") or item.get("lockedText") or item.get("isLocked") or item.get("isReaction") or
                (not photo_ok and (item.get("media") or item.get("mediaCount"))) or
                item.get("mediaFiles") or item.get("previews") or item.get("gif") or item.get("giphyId")):
            raise Refused("source-not-plain-text")
        if isinstance(item.get("price"), bool) or item.get("price") not in (None, 0, 0.0, "0", "0.00"):
            raise Refused("source-not-free-text")
        text = item.get("text")
        if text is None and photo_ok:
            text = ""
        if not isinstance(text, str) or html_to_text(text) != html_to_text(sources[mid]["body"] or ""):
            raise Refused("source-text-changed")
        seen.add(mid)
    if seen != set(sources):
        raise Refused("source-missing")


def check_followup_history(history, anchors, confirmed_ids, *, account, fan, dashboard_handover=False):
    """The follow-up twin of check_provider_history: the only message at or after the anchor (our own last
    outbound) may be that anchor and what this input already sent. Dashboard-managed chats use explicit
    control for handover; a fan reply or anything unreadable still refuses."""
    from twin_bridge.contract import parse_canonical_timestamp
    from twin_bridge.delivery import external_outbound_ids

    if not anchors:
        raise Refused("source-missing")
    since = min(datetime.fromisoformat(r["created_at"]) for r in anchors.values())
    since = since.replace(microsecond=since.microsecond // 1000 * 1000)
    outbound = set(external_outbound_ids(history, set(confirmed_ids) | set(anchors), creator_id=account, fan_id=fan, since=since))
    if outbound and not dashboard_handover:
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
        if stamp < since or mid in confirmed_ids:
            continue
        if dashboard_handover and mid in outbound:
            continue
        if mid not in anchors:
            raise Refused("new-message-during-generation")
        if item.get("isUnsent"):
            raise Refused("source-invalidated")
        seen.add(mid)
    if seen != set(anchors):
        raise Refused("source-missing")


class NotSent(Exception):
    """An admitted send attempt that CERTAINLY did not reach OnlyFans (see ``Ledger.record_not_sent``).

    Raised only from the physical-POST step: either before anything touched the wire (a last-instant hold,
    stop or source check), or after the provider answered the POST with a definite refusal (HTTP 429, or any
    other 4xx but 408: the provider read the request and created nothing, the same rule as
    ``twin_bridge.platform``). A timeout, connection loss, 5xx or an unreadable reply is NOT this: those stay
    ``delivery_unknown``. ``retry`` says whether the same input may be answered again later."""

    def __init__(self, reason, *, retry=False, retry_after=None):
        super().__init__(reason)
        self.reason, self.retry, self.retry_after = reason, retry, retry_after


def _refusal(exc):
    """The ``NotSent`` a provider HTTP error stands for, or None when the outcome is not definite."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if type(status) is not int:
        return None
    if status == 429:
        after = None
        try:
            raw = getattr(exc.response, "headers", {}).get("retry-after")
            try:
                after = float(raw)
            except (TypeError, ValueError):
                target = parsedate_to_datetime(raw)
                after = (target.astimezone(timezone.utc).replace(tzinfo=None) - now()).total_seconds()
            if not math.isfinite(after) or not 0 <= after <= 2**31:
                after = None
        except (TypeError, ValueError, AttributeError):
            pass
        return NotSent("provider-rate-limited", retry=True, retry_after=after)
    if status == 401:
        # The account session is no longer usable. Keep this account-wide: other
        # fans must not each spend a request discovering the same expired login.
        return NotSent("account-auth-failed")
    if 400 <= status < 500 and status != 408:
        return NotSent("provider-refused")
    return None


async def database_call(ledger, function, *args, **kwargs):
    """Offload PostgreSQL lock waits; retain the SQLite execution contract."""
    if getattr(ledger, "pg", False):
        return await asyncio.to_thread(function, *args, **kwargs)
    return function(*args, **kwargs)


class Gateway:
    # Bounded pilots retain their history veto. The dashboard service opts in
    # only when its durable conversation mode is required at the send boundary.
    dashboard_handover = False

    def __init__(self, ledger, pilot, client, loop, content=None, scope=None):
        self.ledger, self.pilot, self.client, self.loop = ledger, pilot, client, loop
        self.content = content
        # pilot.account_id is the Twin/DB-side id; everything said to or read from
        # OnlyFans (history, receipts, client identity) uses the provider-side id.
        self.scope = scope or AccountScope.parse(pilot.account_id)
        from twin_bridge.platform import OnlyFansTextAdapter
        self.reader = OnlyFansTextAdapter(self.scope.provider_id, client=client)
        self.input_key = None
        self.followup = False   # True: the "sources" are our own anchor message (a follow-up), not fan input
        self.sources = {}
        self.bubble = 0
        self.stopped_reason = None
        # The persistent service renews its lease, so the deadline is read from
        # here (never from the frozen Pilot) and a held fan can refuse a bubble.
        self.deadline = pilot.deadline
        self.hold_check = None
        self.contextual = None
        self.not_sent = None   # the NotSent that ended a send (a retryable one lets the caller answer the input again)
        # Discounted resend (service.py arms these for exactly one job): the one unpaid card of this fan the job may
        # remove, and the ids whose removal the provider confirmed. The DELETE happens at the wire, inside the SAME
        # single attempt as the replacement card, so an uncertain outcome is the ledger's ordinary delivery-unknown.
        self.expected_unsend = None
        self.unsend_id = None
        self.unsent_ids = set()

    def arm_unsend(self, message_id):
        """Name the card the next send must remove first. Only the one id the job was built for is accepted."""
        if type(message_id) is not int or message_id != self.expected_unsend or self.unsend_id is not None:
            self.stopped_reason = "unsend-scope-refused"
            raise Refused(self.stopped_reason)
        self.unsend_id = message_id

    def _remove_card(self, message_id):
        """DELETE the armed card at the wire; return only once the provider confirmed it is gone.

        A 404 is FASTT's own "already removed" and counts as removed. Any other failure raises: a definite 4xx
        refusal becomes NotSent in ``_send`` (nothing changed, nothing is sent), anything unproven is delivery-unknown."""
        from of_client import API_BASE
        try:
            res = self.client.delete_json(f"{API_BASE}/messages/{message_id}", json_body={"withUserId": int(self.pilot.fan_id)},
                                          proxy_tries=1, allow_redirects=False)
        except Exception as exc:
            if getattr(getattr(exc, "response", None), "status_code", None) == 404:
                self.unsent_ids.add(message_id)
                return
            raise
        if not (isinstance(res, dict) and res.get("success") is True):
            raise RuntimeError("unsend-unconfirmed")
        self.unsent_ids.add(message_id)

    def abort_reason(self):
        """Cheap, no-database reason a queued sender should leave the gate at once, else None."""
        return self.stopped_reason

    async def pace(self):
        """Wait point before history/admission; the bounded pilot has no pacing."""

    def limit_label(self):
        return self.pilot.end_limit - self.pilot.baseline_used

    def send(self, fan, text, kwargs):
        if type(fan) is not int or fan != self.pilot.fan_id or (kwargs and not self.content) or not isinstance(text, str) or not text.strip():
            self.stopped_reason = "send-scope-refused"
            raise Refused(self.stopped_reason)
        terms = self.content.terms(kwargs) if self.content else None
        return asyncio.run_coroutine_threadsafe(self._send(no_dashes(text), terms), self.loop).result()

    def stopped(self):
        if self.stopped_reason:
            return True
        state = self.ledger.status(self.pilot.pilot_id)
        if state["state"] != "active":
            self.stopped_reason = state["reason"] or "pilot-stopped"
        elif Ledger.counted(state["attempts"]) >= self.pilot.end_limit - self.pilot.baseline_used:
            self.stopped_reason = "send-limit-reached"
        elif now() >= datetime.fromisoformat(self.deadline):
            self.stopped_reason = "time-limit-reached"
        return bool(self.stopped_reason)

    # Transient admission refusals raised before any attempt row exists: the account's gate was taken a moment
    # earlier by another sender (another process, a human send) or the throttle moved. Nothing was written.
    ADMISSION_RETRY = frozenset({"allowance-throttled", "allowance-busy", "allowance-changed",
                                 "provider-concurrency-limit", "conversation-busy", "conversation-throttled",
                                 "admission-contended"})
    send_gate = None            # engine v2: the account's SendGate (service-owned); None = bounded pilot, no gate
    send_wait_max_s = 900.0
    tolerate_newer = False      # engine v2: set for a burst past the hard maximum wait (see Service.turn)
    turn_sends = 0              # engine v2: bubbles already sent this turn (>0 = a continuation at the gate)
    send_priority = None        # engine v2: sort key at the gate (age of the fan's first unanswered message, ISO text)
    throttled_retries = 40
    open_offer_blocks = True    # engine v2: the persistent service sets False (an open offer never stops a text reply)
    commercial_hold_check = None   # R1: the same policy for a priced card / follow-up (adds hardship + complaint cases)
    eligibility_check = None    # engine v2: account-level hold (runtime/policy/credential), re-read at the wire

    async def _send(self, text, terms=None):
        gate = self.send_gate
        if gate is None:
            return await self._send_inner(text, terms)
        gate_key = self.pilot.fan_id
        await gate.acquire(self.abort_reason, self.send_wait_max_s, priority=self.send_priority, key=gate_key,
                           later=self.turn_sends > 0)
        holding = True
        try:
            for tries in range(self.throttled_retries):
                try:
                    sent = await self._send_inner(text, terms)
                    self.turn_sends += 1
                    return sent
                except Refused as exc:
                    if exc.reason not in self.ADMISSION_RETRY or self.stopped_reason:
                        raise
                    if self.turn_sends > 0 and gate.first_waiting():
                        # Throttled with a first bubble queued behind us: give the slot to the fan who has had no
                        # reply yet, then take our place again behind every first bubble.
                        gate.release(gate_key)
                        holding = False
                        await gate.acquire(self.abort_reason, self.send_wait_max_s, priority=self.send_priority,
                                           key=gate_key, later=True)
                        holding = True
                    await asyncio.sleep(min(2.0, 0.25 * (tries + 1)))      # re-pace; the DB gate decides
            self.stopped_reason = "allowance-wait-timeout"
            raise Refused(self.stopped_reason)
        finally:
            if holding:
                gate.release(gate_key)

    def _hold_for(self, terms):
        """The hold check that applies to THIS send: a priced card or a follow-up is a commercial action."""
        if (terms or self.followup or getattr(getattr(self, "content", None), "commercial_text", False)) and self.commercial_hold_check is not None and self.hold_check is not None:
            return self.commercial_hold_check
        return self.hold_check

    def _dispatch_hold(self, terms, attempt):
        check = self._hold_for(terms)
        return check() if check is not None else None

    async def _begin_attempt(self, **kwargs):
        """A contended SQL admission must not block the loop that releases it.

        If cancelled while SQL commits, wait for that result and record the
        definite pre-wire non-send. No dispatch token or POST exists yet.
        """
        pending = asyncio.create_task(database_call(self.ledger, self.ledger.begin_attempt, **kwargs))
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            try:
                admitted = await pending
            except Exception:
                raise asyncio.CancelledError from None
            await asyncio.shield(database_call(self.ledger, self.ledger.record_not_sent,
                pilot_id=self.pilot.pilot_id, owner_token=self.pilot.owner_token,
                attempt_id=admitted.id, now=now()))
            raise
        except store.DB_ERRORS as exc:
            # The transaction rolled back before any wire attempt. Only these
            # explicit contention outcomes may re-enter bounded admission.
            if getattr(exc, "pgcode", None) in {"55P03", "40P01", "40001"}:
                raise Refused("admission-contended") from None
            raise

    async def _send_inner(self, text, terms=None):
        from twin_bridge.coordination import authorize_dispatch, revoke_dispatch, guarded_send
        from twin_bridge.platform import escape_plain_text, validate_created_message
        from twin_bridge.normalize import html_to_text

        attempt = None
        contextual_metadata = None
        settled = False
        unsend_id, self.unsend_id = self.unsend_id, None      # armed for THIS send only, never carried to another
        try:
            if self.stopped() or not self.input_key:
                raise Refused(self.stopped_reason or "pilot-stopped")
            if unsend_id is not None and not terms:
                raise Refused("unsend-needs-card")
            if len(text.encode("utf-16-le")) // 2 > 4000:
                raise Refused("output-too-long")
            from .media_claims import PLACEHOLDER_HELD, echoes_instruction
            if echoes_instruction(text):
                # Last line of defence on the exact wire payload (after any rewrite/split/caption composition).
                raise Refused(PLACEHOLDER_HELD)
            draft_ready_at = now()
            await self.pace()
            throttle_done_at = now()
            if self.stopped():
                raise Refused(self.stopped_reason)
            since = min(datetime.fromisoformat(r["created_at"]) for r in self.sources.values())
            history = await self.reader.preflight_history(self.pilot.fan_id, since=since, source_ids=set(self.sources))
            receipts = self.ledger.receipts(pilot_id=self.pilot.pilot_id, input_key=self.input_key)
            known = {int(r["provider_message_id"]) for r in receipts if r["state"] == "sent"}
            (check_followup_history if self.followup else check_provider_history)(
                history, self.sources, known, account=self.scope.provider_id, fan=self.pilot.fan_id,
                dashboard_handover=self.dashboard_handover, **({"tolerate_newer": True} if self.tolerate_newer and not self.followup else {}))
            check_local(self.ledger.path, self.pilot.account_id, self.pilot.fan_id,
                            approved_item=self.content.catalog_ids if self.content else None,
                            open_offer_blocks=self.open_offer_blocks)
            check = self._hold_for(terms)
            if check is not None:
                held = await asyncio.to_thread(check)
                if held:
                    raise Refused(held)
            if self.eligibility_check is not None:
                held = await asyncio.to_thread(self.eligibility_check)
                if held:
                    raise Refused(held)
            if self.content:
                with readonly(self.ledger.path) as con:
                    self.content.check_catalog(con, self.pilot.account_id)
                    if terms:
                        terms = self.content.check_send(con, self.pilot.account_id, terms)
                        from .contextual import refuse_repeat_media
                        if unsend_id is None:
                            refuse_repeat_media(con, self.pilot.account_id, self.pilot.fan_id, terms)
                if terms:
                    for media_id in terms["media_ids"]:
                        asset = await asyncio.to_thread(self.client.vault_media_by_id, media_id)
                        if (str(asset.get("id")) != str(media_id) or asset.get("type") != terms["media_types"][str(media_id)]
                                or asset.get("canView") is not True):
                            raise Refused("content-asset-unavailable")
            bubble = self.bubble + 1
            dispatch_kwargs = {}
            if self.tolerate_newer and not self.followup:
                dispatch_kwargs["tolerate_newer"] = True
            if getattr(self, "reply_decision", None):
                dispatch_kwargs["reply_authority"] = self.reply_decision["authority"]
            attempt = await self._begin_attempt(pilot_id=self.pilot.pilot_id, owner_token=self.pilot.owner_token,
                input_key=self.input_key, bubble_no=bubble, revision=self.pilot.revision, now=now(),
                **dispatch_kwargs)
            self.bubble = bubble  # refused admission did not create a bubble
            from .recovery import freeze
            try:
                if terms and getattr(self, "contextual", None) is not None and self.contextual.armed is not None:
                    contextual_metadata = await database_call(self.ledger, self.contextual.reserve, terms, attempt.id)
                    terms = {**terms, "contextual": contextual_metadata}
                await database_call(self.ledger, freeze, self.ledger, pilot_id=self.pilot.pilot_id, owner_token=self.pilot.owner_token,
                       attempt_id=attempt.id, body=text, terms=terms)
            except Exception:
                raise NotSent("dispatch-proof-not-saved", retry=True) from None
            token = authorize_dispatch(self.pilot.account_id, self.pilot.fan_id, turn_id=attempt.turn_id,
                attempt_no=attempt.attempt_no, owner_token=self.pilot.owner_token, revision=self.pilot.revision)

            wire = [False]
            wire_at = [None]

            def physical_post():
                # Re-read stop/owner after the thread handoff, immediately at the wire.
                with readonly(self.ledger.path) as con:
                    row = con.execute("SELECT state,owner_token,revision,owner_expires_at,reserved FROM twin_bridge_conversations WHERE account_id=? AND fan_id=?",
                                      (self.pilot.account_id, self.pilot.fan_id)).fetchone()
                    p = con.execute("SELECT state FROM native_pilots WHERE pilot_id=?", (self.pilot.pilot_id,)).fetchone()
                    if (not row or row["state"] != "active" or not row["reserved"] or row["owner_token"] != self.pilot.owner_token or
                            row["revision"] != self.pilot.revision or not p or p["state"] != "active" or
                            not row["owner_expires_at"] or now() >= datetime.fromisoformat(row["owner_expires_at"]) or
                            now() >= datetime.fromisoformat(self.deadline)):
                        raise Refused("authority-lost-before-post")
                    if terms and self.content:
                        # Revalidate direct requests as well as contextual offers
                        # after provider asset reads and immediately before POST.
                        self.content.check_send(con, self.pilot.account_id, terms)
                        if unsend_id is None:
                            from .contextual import refuse_repeat_media
                            refuse_repeat_media(con, self.pilot.account_id, self.pilot.fan_id, terms,
                                                own_attempt_id=attempt.id if contextual_metadata is not None else None)
                self.ledger.validate_dispatch(pilot_id=self.pilot.pilot_id,
                    owner_token=self.pilot.owner_token, attempt_id=attempt.id, now=now(),
                    **dispatch_kwargs)
                if self._dispatch_hold(terms, attempt):
                    raise Refused("fan-held-before-post")
                if contextual_metadata is not None:
                    self.contextual.before_post(contextual_metadata)
                wire_at[0] = now()
                with guarded_send(self.pilot.account_id, self.pilot.fan_id, purpose="twin_bridge", dispatch_token=token):
                    if unsend_id is not None:
                        wire[0] = True    # the DELETE may have reached OnlyFans from here on
                        self._remove_card(unsend_id)
                    if terms:
                        from of_client import API_BASE
                        body = {"text": escape_plain_text(text), "lockedText": False,
                                "mediaFiles": list(terms["media_ids"]), "price": terms["price_cents"] / 100,
                                "previews": list(terms["preview_ids"]), "rfTag": [], "rfGuest": [], "rfPartner": [],
                                "isForward": False}
                        wire[0] = True   # from the next call on, the POST may have reached OnlyFans
                        return self.client.post_json(f"{API_BASE}/chats/{self.pilot.fan_id}/messages", json_body=body, proxy_tries=1,
                                                    allow_redirects=False)
                    wire[0] = True
                    return self.client.send_text_once_body(self.pilot.fan_id, escape_plain_text(text))
            try:
                raw = await asyncio.to_thread(physical_post)
            except Exception as exc:
                # Certain outcomes only (see NotSent); anything else propagates as the unknown it is.
                if not wire[0]:
                    raise NotSent(exc.reason if isinstance(exc, Refused) else "send-check-failed", retry=True) from None
                refused = _refusal(exc)
                if refused is not None:
                    raise refused from None
                raise
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
            await database_call(self.ledger, self.ledger.record_sent, pilot_id=self.pilot.pilot_id, owner_token=self.pilot.owner_token,
                attempt_id=attempt.id, provider_message_id=validated.provider_message_id,
                validated_response=raw, evidence={**validated.evidence, **({"content": content_evidence} if terms else {})}, now=now())
            settled = True
            if contextual_metadata is not None:
                await database_call(self.ledger, self.contextual.outcome, "sent", int(validated.provider_message_id))
            decision = getattr(self, "reply_decision", None)
            if decision and decision["schedule"] and not known:
                from .pacing import delivery_event
                try:
                    emit(delivery_event(decision["authority"], decision["schedule"], receipt_at=now(),
                                        wire_at=wire_at[0], provider_created_at=raw.get("createdAt"),
                                        admitted_at=decision.get("admitted_at"), draft_ready_at=draft_ready_at,
                                        throttle_done_at=throttle_done_at, eligible_at=decision["eligible_at"]))
                except Exception:
                    emit({"event": "reply-pacing-metric-error"})
            emit({"event": "sent", "engine": "fastt-native", "provider_message_id": validated.provider_message_id,
                  "pilot_sent": len([r for r in self.ledger.receipts(pilot_id=self.pilot.pilot_id) if r["state"] == "sent"]),
                  "limit": self.limit_label()})
            return raw
        except Exception as exc:
            if attempt is None and self.send_gate is not None and isinstance(exc, Refused) and exc.reason in self.ADMISSION_RETRY:
                raise
            self.stopped_reason = exc.reason if isinstance(exc, (Refused, NotSent)) else "send-check-failed"
            if attempt is not None and not settled:
                certain = False
                if isinstance(exc, NotSent):
                    try:
                        await database_call(self.ledger, self.ledger.record_not_sent, pilot_id=self.pilot.pilot_id, owner_token=self.pilot.owner_token,
                                                    attempt_id=attempt.id, now=now())
                        certain, self.not_sent = True, exc
                    except Exception:
                        pass   # could not settle it as certain: keep the old, safe reading below
                if not certain:
                    await database_call(self.ledger, self.ledger.record_unknown, pilot_id=self.pilot.pilot_id, owner_token=self.pilot.owner_token,
                                               attempt_id=attempt.id, now=now())
                    self.stopped_reason = "delivery-unknown"
            if contextual_metadata is not None and not settled:
                # A failed evidence write is not proof that nothing went out.
                await database_call(self.ledger, self.contextual.outcome, "cancelled" if self.not_sent else "unknown")
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
    with store.transaction(path, account) as con:
        return store.insert_id(con, "INSERT INTO automation_runs(account_id,kind,started_at,status) VALUES(?,'ai_chatter',?,'running')",
                               (account, now().isoformat(sep=" ")))


def finish_native_run(path, run_id, stats, reason=None, *, account=None):
    # Only aggregate counters/fixed reasons, never native prompt/error text.
    counters = {k: v for k, v in (stats or {}).items() if isinstance(k, str) and type(v) in (int, bool)}
    with store.transaction(path, account) as con:
        con.execute("UPDATE automation_runs SET completed_at=?,status=?,stats_json=?,error_text=? WHERE id=?",
                    (now().isoformat(sep=" "), "error" if reason else "done", json.dumps(counters), reason, run_id))


NO_EMPLOYEE_CHECK = object()


def verify_attribution(path, account, fan, receipts, content=None, automation_employee_id=NO_EMPLOYEE_CHECK):
    """Every accepted send must be a stored AI row; with an employee id given it must
    carry exactly the Automation sentinel (never NULL, never a human's id)."""
    with readonly(path) as con:
        for receipt in receipts:
            if receipt["state"] == "not_sent":
                continue  # certainly never sent: there is no message to attribute
            if receipt["state"] != "sent":
                raise Refused("delivery-unresolved")
            row = con.execute("SELECT direction,automation_kind,price_cents,media_count"
                              + ("" if automation_employee_id is NO_EMPLOYEE_CHECK else ",sent_by_employee_id")
                              + " FROM messages WHERE account_id=? AND fan_id=? AND message_id=?",
                              (account, fan, int(receipt["provider_message_id"]))).fetchone()
            stored = con.execute("SELECT response_json FROM native_pilot_receipts WHERE attempt_id=?", (receipt["attempt_id"],)).fetchone() if content else None
            raw = json.loads(stored[0]) if stored else {}
            cents = int(round(float(raw.get("price") or 0) * 100)) if content else 0
            # Vault mode: FASTT's own pack lane stamps its cards "pack_send" (pack_pricing.PACK_ATTR_KIND).
            kinds = (("ai_chatter", "ai_upseller") + (("pack_send",) if getattr(content, "vault_mode", False) else ())
                     if content else ("ai_chatter",))
            if not row or row["direction"] != "out" or row["automation_kind"] not in kinds or row["price_cents"] != cents or (not content and row["media_count"] != 0):
                raise Refused("native-attribution-missing")
            if automation_employee_id is not NO_EMPLOYEE_CHECK and row["sent_by_employee_id"] != automation_employee_id:
                raise Refused("native-attribution-employee")


async def run(args):
    from db.engine import engine
    from twin_bridge.coordination import store_target, reserve_ownership, clear_owner
    from .attribution import read_sentinels, require_sentinels
    from .budget import DailyBudget
    from .connection import build_client, load_verified_connection
    from .engine import NativeEngine
    from .model import NativePilotModel, MODEL, ENDPOINT

    # The one account-id boundary: `db` for Twin's tables/FASTT's queries/the ledger,
    # `scope.provider_id` for OnlyFans. Live data keeps its `of-<owner>` ids.
    scope = AccountScope.parse(args.account)
    db = scope.db_id
    path = store_target()
    if not path:
        raise Refused("pilot-store-required")
    require_store_schema(path)
    profile = check_local(path, db, args.fan)
    from .content import ContentPlan
    vault_mode = bool(getattr(args, "vault_mode", False))
    if vault_mode and getattr(args, "content_manifest", None):
        raise Refused("vault-mode-and-manifest")
    if vault_mode:
        from .content import VaultPlan
        content = VaultPlan(db, args.fan, path)
    else:
        content = ContentPlan(args.content_manifest, db, args.fan) if getattr(args, "content_manifest", None) else None
    if content and not vault_mode:
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
        if content and content.enables_catalog:
            # The reviewed card must be sellable for this run. Remember the live
            # flag so it is restored, not forced, when the pilot ends.
            with store.transaction(path) as con:
                prior_catalog = {}
                for item_id in sorted(content.catalog_ids):
                    row = con.execute("SELECT enabled FROM catalog_items WHERE account_id=? AND id=?",
                                      (db, item_id)).fetchone()
                    if row is None:
                        raise Refused("content-catalog-scope")
                    prior_catalog[item_id] = row[0]
                for item_id in prior_catalog:
                    con.execute("UPDATE catalog_items SET enabled=? WHERE account_id=? AND id=?", (True, db, item_id))
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
                if Ledger.counted(attempts) >= args.send_limit:
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
                    # Bounded TEST pilot only (never the persistent service): it ends once ANY reviewed
                    # paid card of its manifest is confirmed, whatever that item's price (content.paid).
                    if any(x and x["price_cents"] > 0 for x in sent_media):
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
                    with store.transaction(path) as con:
                        for item_id, enabled in prior_catalog.items():
                            con.execute("UPDATE catalog_items SET enabled=? WHERE account_id=? AND id=?",
                                        (enabled, db, item_id))
        finally:
            try:
                if client is not None:
                    from of_client import close_client_http
                    close_client_http(client)
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
    parser.add_argument("--vault-mode", action="store_true",
                        help="FASTT's stock vault selling (default off): cards of live vault_items media within FASTT's price "
                             "bounds; mutually exclusive with --content-manifest")
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
