"""FasttSendGate: the only send path FASTT's ai_chatter has inside the runner process.

ai_chatter keeps its own decisions (what to say, whether to attach the approved
card, how long to type). It calls `client.send_message(fan, text, price=, media_files=,
previews=)` on whatever `ax._make_client` returns. In the runner that is this gate.
Every call becomes one durable TextAuthority intent with origin 'fastt':

  prepare  -> revisions captured at TURN START are re-validated (control + input)
  admit    -> the account cap, throttle, hold and ownership checks, then the wire marker
  transport.send -> consume_dispatch (final recheck) then exactly ONE POST, no retry
  record_accepted + materialize -> receipt, messages row, delivery row, offer/vault/quote rows

An uncertain POST is held (never replayed). Any pause, takeover, newer inbound, or other
revision change before a bubble -- including after pacing and throttle waits -- raises a
typed stop that unwinds ai_chatter's whole run. Nothing here selects what to sell.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import threading
import time
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import CATALOG_IS_SINGLE, CatalogItem, OfConversationState
from db.onlyfans_models import OfControlAudit
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from .fastt_errors import FasttHalt, FasttStop, FasttTurnAbort
from .store import Refused, Store
from .text_delivery import TextAuthority

ACTOR = "fastt-runner"
STOP_ACTOR = "fastt-runner:stop-after-paid-card"
STOP_KEY_PREFIX = "fastt-stop:"
# Refusal texts that mean "a send is unresolved": the runner must halt, not just skip this turn.
_HALT = {"unresolved_delivery", "account delivery unresolved", "cap_exhausted", "history_quarantined"}
_ALLOWED_KWARGS = {"price", "locked_text", "media_files", "previews"}


class FasttCapabilityDisabled(RuntimeError):
    """A client capability the pilot does not grant (reads, stickers, schedules, mass sends)."""


@dataclass
class Turn:
    turn_id: str
    control_revision: int
    input_revision: int
    sent: int = 0
    seq: int = 0
    stopped: bool = False


def _cents(price):
    try:
        cents = round(float(price) * 100)
    except (TypeError, ValueError):
        raise FasttTurnAbort("invalid_price") from None
    if cents < 0:
        raise FasttTurnAbort("invalid_price")
    return int(cents)


class FasttSendGate:
    def __init__(self, store: Store, authority: TextAuthority, transport, *, tenant_id, account_id, fan_id,
                 actor=ACTOR, throttle_wait_seconds=120.0, stop_event: threading.Event | None = None):
        if type(fan_id) is not int or fan_id <= 0:
            raise Refused("invalid runner fan scope")
        self.store, self.authority, self.transport = store, authority, transport
        self.tenant_id, self.account_id, self.fan_id = tenant_id, account_id, fan_id
        self.actor, self.throttle_wait = actor, float(throttle_wait_seconds)
        self.stop_event = stop_event or threading.Event()
        self.turn: Turn | None = None
        self.accepted: list[dict] = []
        self.dry_run = False          # provider writes disabled: record what would be sent, send nothing
        self.would_send: list[dict] = []

    # -- capabilities the pilot does not grant ------------------------------------------------------
    def mark_chat_read(self, chat_id, *a, **kw):
        return {}  # read indicators are disabled in the runner

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        raise FasttCapabilityDisabled(f"runner client does not grant {name}")

    # -- turn lifecycle -----------------------------------------------------------------------------
    def begin_turn(self, *, expected_input_revision=None) -> Turn:
        """Capture control/input revisions NOW (before generation and pacing) for this turn."""
        with Session(self.store.engine) as s:
            state = s.get(OfConversationState, (self.account_id, self.fan_id))
            if state is None or state.mode != "automated" or state.takeover_state != "none":
                raise FasttTurnAbort("not_automated")
            if expected_input_revision is not None and state.input_revision != expected_input_revision:
                raise FasttTurnAbort("input_changed_before_turn")
            self.turn = Turn(uuid.uuid4().hex[:16], state.revision, state.input_revision)
        return self.turn

    def end_turn(self):
        self.turn = None

    # -- card resolution ----------------------------------------------------------------------------
    def _resolve_card(self, media_files, previews, cents):
        try:
            media = sorted(int(x) for x in media_files)
            preview = sorted(int(x) for x in (previews or []))
        except (TypeError, ValueError):
            raise FasttTurnAbort("invalid_media") from None
        with Session(self.store.engine) as s:
            rows = s.scalars(select(CatalogItem).where(CatalogItem.account_id == self.account_id,
                CATALOG_IS_SINGLE, CatalogItem.enabled.is_(True), CatalogItem.is_free_teaser.is_(False))).all()
            found = []
            for item in rows:
                try:
                    ids = sorted(json.loads(item.media_ids))
                    prev = sorted(json.loads(item.preview_media_ids))
                except (TypeError, ValueError):
                    continue
                if ids == media and prev == preview and int(item.price_cents or 0) == cents:
                    found.append(item.id)
        if len(found) != 1:
            # No unique approved item with exactly these files, previews and fixed price: never guess.
            raise FasttTurnAbort("card_not_in_approved_catalogue")
        return found[0]

    # -- the send -----------------------------------------------------------------------------------
    def send_message(self, chat_id, text="", *, giphy_id=None, **kw):
        turn = self.turn
        if turn is None:
            raise FasttHalt("send_outside_turn")
        if str(chat_id) != str(self.fan_id):
            raise FasttHalt("scope_violation")
        if giphy_id is not None or set(kw) - _ALLOWED_KWARGS or kw.get("locked_text"):
            raise FasttTurnAbort("unsupported_send_form")
        if self.dry_run:
            self.would_send.append({"text": text, "price": kw.get("price", 0), "media": list(kw.get("media_files") or [])})
            raise FasttTurnAbort("dry_run_provider_writes_disabled")
        if turn.stopped:
            raise FasttTurnAbort("stopped_after_paid_card")
        if self.stop_event.is_set():
            raise FasttTurnAbort("runner_stopping")
        if not isinstance(text, str) or not text.strip():
            raise FasttTurnAbort("empty_text")
        media_files, previews = kw.get("media_files"), kw.get("previews")
        cents = _cents(kw.get("price", 0))
        item_id = None
        if media_files:
            if cents <= 0:
                raise FasttTurnAbort("free_media_not_granted")
            item_id = self._resolve_card(media_files, previews, cents)
        elif cents or previews:
            raise FasttTurnAbort("price_without_media")
        turn.seq += 1
        key = f"fastt:{turn.turn_id}:{turn.seq}"
        try:
            intent_id = self.authority.prepare(
                self.tenant_id, self.account_id, self.fan_id, command_key=key,
                expected_revision=turn.control_revision, expected_input_revision=turn.input_revision + turn.sent,
                body=text, origin="fastt", actor=self.actor,
                catalog_item_id=item_id, quoted_price_cents=cents if item_id is not None else None)
        except Refused as exc:
            reason = str(exc)[:80]
            raise (FasttHalt if reason in _HALT else FasttTurnAbort)(reason) from None
        row = self._dispatch_until_settled(intent_id)
        result = {"id": row.provider_message_id, "text": row.body,
                  "createdAt": json.loads(row.receipt_json)["occurred_at"]}
        turn.sent += 1
        self.accepted.append({"intent_id": intent_id, "message_id": row.provider_message_id,
                              "catalog_item_id": row.catalog_item_id})
        if row.catalog_item_id is not None:
            # The approved paid card is accepted and projected: make the stop durable BEFORE ai_chatter
            # can send anything else (it also refuses any later send in this turn).
            turn.stopped = True
            self.stop_after_card(intent_id)
        return result

    def _dispatch_until_settled(self, intent_id):
        deadline = time.monotonic() + self.throttle_wait
        while True:
            try:
                status = self.authority.dispatch(self.tenant_id, self.account_id, intent_id, self.transport)
            except FasttStop:
                raise
            except Exception as exc:
                # Anything raised after a wire grant is uncertain; the durable row already holds it.
                raise FasttHalt("dispatch_error:" + type(exc).__name__) from None
            row = self._row(intent_id)
            if status == "accepted":
                if row.projected_at is None or row.provider_message_id is None:
                    raise FasttHalt("projection_pending")
                return row
            if status == "queued":
                wait = self._throttle_remaining() if row.reason == "account_throttled" else None
                if wait is None or time.monotonic() + wait > deadline:
                    self.authority.cancel_queued(self.tenant_id, self.account_id, intent_id, "fastt_not_dispatched")
                    raise FasttTurnAbort(row.reason or "queued")
                if self.stop_event.wait(wait + 0.05):
                    self.authority.cancel_queued(self.tenant_id, self.account_id, intent_id, "fastt_stopping")
                    raise FasttTurnAbort("runner_stopping")
                continue
            if status in {"cancelled", "refused"}:
                reason = (row.reason or status)[:80]
                raise (FasttHalt if reason in _HALT else FasttTurnAbort)(reason)
            # unknown, not_accepted, anything else: held, never replayed.
            raise FasttHalt("unknown_send" if status == "unknown" else f"send_{status}")

    def _row(self, intent_id):
        with Session(self.store.engine) as s:
            row = s.get(OfTextIntent, (self.account_id, intent_id))
            s.expunge(row)
            return row

    def _throttle_remaining(self):
        with Session(self.store.engine) as s:
            policy = s.get(OfDeliveryPolicy, self.account_id)
            if policy is None or policy.next_dispatch_at is None:
                return 0.0
            now = self.authority._db_now(s)
            return max(0.0, (policy.next_dispatch_at - now).total_seconds())

    # -- durable stop after the approved paid card --------------------------------------------------
    def stop_after_card(self, intent_id):
        """Pause AI for this fan through the one control path (Store.control), attributed to the runner.

        TODO(Y/control mapping): Store.control is expected to project mode != automated into
        skip_list(manual_restrict) in the same transaction; nothing extra is needed here once it does.
        Idempotent: the command key is derived from the card's intent id.
        """
        apply_stop(self.store, self.tenant_id, self.account_id, self.fan_id, intent_id)


def apply_stop(store: Store, tenant_id, account_id, fan_id, intent_id):
    key = STOP_KEY_PREFIX + intent_id
    for _ in range(3):
        with Session(store.engine) as s:
            if s.get(OfControlAudit, (account_id, key)) is not None:
                return False  # already applied (even if an operator resumed since)
            state = s.get(OfConversationState, (account_id, fan_id))
            if state is None or state.mode != "automated":
                return False  # a human / earlier stop already owns the conversation
            revision = state.revision
        try:
            store.control(tenant_id, account_id, fan_id, mode="paused", expected_revision=revision,
                          command_key=key, actor=STOP_ACTOR,
                          reason="FASTT pilot: approved paid card accepted; AI stops until an operator resumes",
                          action="pause_automation")
            return True
        except Refused:
            continue  # revision moved (e.g. our own receipt projection); re-read and retry
    raise FasttHalt("stop_not_applied")


def reconcile_stop(store: Store, tenant_id, account_id, fan_id):
    """Crash repair: any accepted 'fastt' paid card whose stop was never recorded gets its stop now."""
    with Session(store.engine) as s:
        cards = s.scalars(select(OfTextIntent.intent_id).where(
            OfTextIntent.account_id == account_id, OfTextIntent.fan_id == fan_id,
            OfTextIntent.origin == "fastt", OfTextIntent.catalog_item_id.is_not(None),
            OfTextIntent.status == "accepted").order_by(OfTextIntent.created_at)).all()
    applied = False
    for intent_id in cards:
        applied = apply_stop(store, tenant_id, account_id, fan_id, intent_id) or applied
    return applied
