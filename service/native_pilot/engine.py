"""Process-local, text-only adapter around Fastt's actual ai_chatter engine.

This context belongs in an isolated pilot process, never a running application
server. It does not persist enablement, import Twin, or replace Fastt's prompts,
history gathering, candidate gates, reply shaping, leases or attribution.
The runner owns fresh-input admission, transport, durable send cap and recovery.
"""
from __future__ import annotations

import asyncio
import copy
from datetime import timedelta
from types import SimpleNamespace

from .scope import AccountScope
from .followups import NUDGE_KIND, DISCOUNT_KIND, POST_BUY_KIND, nudge_job, job_kind, nudge_payload

MODEL = "deepseek/deepseek-v4.1-flash"
RESOLVER_PURPOSE_PREFIX = "content_resolver"

# Independent switches are explicit: disabling the sales catalogue alone does
# not turn off Fastt's teaser, pack, sticker or scheduled follow-up paths.
# Stock FASTT (ai_chatter._DEFAULTS) everywhere except what this boundary cannot carry
# yet. Each override below has a concrete reason; anything not listed runs as stock.
TEXT_ONLY = {
    "enabled": True, "mode": "always",
    "max_fans_per_tick": 1,            # one runner turn serves exactly one fan
    "payers_only": False,              # stock True would ignore every non-payer
    "pack_send_enabled": False, "pack_on_ask_enabled": False,  # packs/gifts send media outside the
    "gift_enabled": False,                                     # approved catalogue: refused at the send boundary
    "qualification_gate_enabled": False,                      # the full legacy ladder stays suppressed
    "nudge_enabled": False, "rhythm_enabled": False,  # need FASTT's background job runner; jobs are
    "post_buy_rung_enabled": False,                   # suppressed here (rhythm would "brb" and never return)
    "rhythm_return_single_bubble_s": 0,
    "profile_openers_enabled": False,  # first-contact openers: new conversations stay operator-controlled
    "cadence_enabled": False,          # the runner claims each input once; a cadence skip ("rest next
                                       # tick" in stock FASTT) would leave that message unanswered for good
}

# ``--followups``: FASTT's stock re-engage nudge. ai_chatter gates it as ``cadence_on and nudge_enabled``
# (both when scheduling it and in _run_nudge), so the cadence controller has to be ON. It is made inert
# rather than reimplemented: every burst/daily cap is 0 ("no cap" in FASTT's _cadence_gate), so it never
# skips a fan (the "rest next tick" skip that TEXT_ONLY avoids); no spend floor, no daily quota.
FOLLOWUPS = {
    "nudge_enabled": True, "cadence_enabled": True,
    "msg_limits_by_signal": {"baseline": 0, "buying_signal": 0, "no_signal": 0, "pic_sent": 0},
    "msg_limits_by_spend": [],
    "daily_quota_enabled": False, "daily_quota_enforce": False,
}


class PilotBoundaryError(RuntimeError):
    """Fixed public reason only: never embed message text or transport details."""


class _ResolverRefused(Exception):
    """Raised by the resolver's client. Deliberately not a PilotBoundaryError: FASTT catches it
    (delivery reaction not sent, expiry unsend skipped) and the run is not marked violated."""


class _ResolverClient:
    """What FASTT's unlock watcher gets: no platform reads or writes at all. Paid state comes from the
    ledger/transactions tables; the delivery reaction, unsend and live re-read all fail closed."""

    def send_message(self, *args, **kwargs):
        raise _ResolverRefused("resolver-send-refused")

    def unsend_message(self, *args, **kwargs):
        raise _ResolverRefused("resolver-unsend-refused")

    def get_messages(self, *args, **kwargs):
        raise _ResolverRefused("resolver-read-refused")


class _TextClient:
    """No wildcard delegation to a live client; its only write is plain text."""

    def __init__(self, engine):
        self.engine = engine

    def send_message(self, fan_id, text, **kwargs):
        # Native plain-text calls have no keyword arguments. Refuse even an
        # empty media/price/quote argument, rather than guessing its semantics.
        if (type(fan_id) is not int or fan_id != self.engine.fan_id
                or not isinstance(text, str) or not text.strip() or (kwargs and not self.engine.content)):
            self.engine._refuse("forbidden-platform-operation")
        if self.engine.content:
            self.engine.content.terms(kwargs)
        try:
            result = self.engine._send(fan_id, text, kwargs)
        except Exception:
            # The native loop intentionally catches send exceptions. Preserve a
            # stop signal outside that loop, without logging raw transport text.
            self.engine._refuse("native-send-interrupted")
        if kwargs:
            self.engine._card_sent = True      # a priced card (media + price) reached the wire
        return result

    def mark_chat_read(self, fan_id):
        if type(fan_id) is not int or fan_id != self.engine.fan_id:
            self.engine._refuse("wrong-fan")
        # Reading indicators are not required for this message-only experiment.
        return None


class _DiscountClient(_TextClient):
    """The text client of a discounted-resend job and of nothing else: the only one with a removal surface, and that
    surface names the one card the next send removes at the wire, it never touches the platform itself."""

    def unsend_message(self, message_id, with_user_id=None):
        """The discounted resend's removal of its OWN unpaid vault card, and nothing else. It does not touch the
        platform here: it names the one card the next send must remove first, at the wire, inside the same single
        attempt as the replacement (``runner.Gateway._remove_card``). Any other use is a boundary violation."""
        e = self.engine
        if (e._job != DISCOUNT_KIND or e._arm_unsend is None or type(message_id) is not int
                or with_user_id != e.fan_id):
            e._refuse("forbidden-platform-operation")
        try:
            e._arm_unsend(message_id)
        except Exception:
            e._refuse("forbidden-platform-operation")
        return {"success": True, "deferred": True}

    def removed(self, message_id):
        """True once the provider confirmed this card is gone (the wire step above ran and succeeded)."""
        return self.engine._unsent is not None and message_id in self.engine._unsent()


# Offer limits for the reviewed catalogue: two open / two per day unless the
# operator saved explicit FASTT limits on the account (ai_chatter_config_json).
# Separate keys, so an older generic ai_chatter value never changes FASTT selling.
OFFER_LIMITS = {"fastt_max_open_offers": ("max_open_offers", 2, 2, 10),
                "fastt_max_offers_per_day": ("max_offers_per_fan_per_day", 2, 0, 50)}


# What the model is told when its offer was declined (cap, repeat): chat on, never price.
NO_OFFER_NOTE = ("No offer, price or unlock is possible this turn. Reply naturally to what he just said; "
                 "do not mention prices, unlocking, a video, a pic or anything to buy.")


REPEAT_REASONS = ("item-already-pending", "media-already-pending")


def _with_content(result, content):
    """The model result with its text replaced (the marker stripped), whatever its concrete type."""
    try:
        clone = copy.copy(result)
        clone.content = content
        return clone
    except Exception:
        return SimpleNamespace(content=content, parsed=getattr(result, "parsed", None))


def offer_limits(native):
    out = {}
    for key, (target, default, lo, hi) in OFFER_LIMITS.items():
        value = native.get(key, default)
        out[target] = value if type(value) is int and lo <= value <= hi else default
    return out


class NativeEngine:
    """Scoped Telegram capabilities around the real native engine.

    This class used to monkeypatch twelve module globals.  It now implements the
    production capability contract consumed explicitly by ``ai_chatter``.  The
    context token is reversible and task-local; no second conversation engine
    or hidden process-global routing table exists.
    """

    _active = False

    def __init__(self, account_id, fan_id, *, chat, send, emit=None, model=MODEL, content=None, context=None,
                 own_offers_only=False, followups=None, arm_unsend=None, unsent=None, card_of=None,
                 unanswered_reply_ids=()):
        # ``account_id`` is the DB-side id ai_chatter queries with: Twin's
        # ``of-<owner>`` or a bare decimal (see native_pilot.scope).
        if not AccountScope.valid(account_id) or type(fan_id) is not int or fan_id <= 0:
            raise ValueError("invalid-pilot-identity")
        self.account_id = account_id
        self.fan_id = fan_id
        if any(type(mid) is not int or mid <= 0 for mid in unanswered_reply_ids):
            raise ValueError("invalid-unanswered-reply-ids")
        self._unanswered_replies = frozenset((fan_id, mid) for mid in unanswered_reply_ids)
        if not isinstance(model, str) or not model.strip():
            raise ValueError("invalid-pilot-model")
        self.model = model
        self.content = content
        # Optional Twin approved-context source for the single prompt hook.
        self.context = context
        # All-fans runner: another fan's open offer is that fan's business, not this turn's obligation.
        self.own_offers_only = bool(own_offers_only)
        # Runner-owned queue (followups.FollowupQueue) or None. None = every background job is suppressed.
        self.followups = followups
        self._chat = chat
        self._send = send
        # Discounted resend only: the gateway's guarded removal (see _TextClient.unsend_message) and the ids it confirmed gone.
        self._arm_unsend, self._unsent = arm_unsend, unsent
        self._card_of = card_of    # message id -> the vault card as sent (media / previews / price) from the runner's receipts
        self._job = None           # the follow-up kind this run serves (nudge / discount / post_buy), None for a fan turn
        self._emit = emit or (lambda event: None)
        self._token = None
        self._running = False
        self._violation = None
        self._offer_cfg = {"max_open_offers": 2, "max_offers_per_fan_per_day": 2}
        # --followups in vault mode: FASTT's own nudge delay and what this turn did (see _queue_card_nudge).
        self._nudge_min, self._card_sent, self._nudge_queued = 0, False, False

    def _refuse(self, reason):
        self._violation = reason
        raise PilotBoundaryError(reason)

    def _account(self, account_id):
        if str(account_id) != self.account_id:
            self._refuse("wrong-account")

    async def _no_obligations(self):
        from sqlalchemy import select
        from db.engine import get_session
        from db.models import ContentOffer
        query = select(ContentOffer.id).where(
            ContentOffer.account_id == self.account_id,
            ContentOffer.status.in_(("open", "unlocked")),
        )
        if self.own_offers_only:
            query = query.where(ContentOffer.fan_id == self.fan_id)
        ids = getattr(self.content, "catalog_ids", None) if self.content is not None else None
        if ids:
            # Approved PPVs are unlocked by OnlyFans. Any other item or a tip-unlock
            # remains an obligation; in all-fans mode the query is already fan-scoped.
            approved = (ContentOffer.fan_id == self.fan_id) & (ContentOffer.mode == "ppv") \
                & ContentOffer.item_id.in_(sorted(ids))
            query = query.where(~approved)
        async with get_session() as session:
            row = (await session.execute(query.limit(1))).first()
        if row is not None:
            self._refuse("open-offer-obligation")

    async def load_config(self, account_id, native):
        self._account(account_id)
        result = dict(native)
        sales_enabled = result.pop("_native_on_request_sales_enabled", False) is True
        result.update(TEXT_ONLY)
        if self.followups is not None:
            result.update(copy.deepcopy(FOLLOWUPS))
            minutes = result.get("nudge_after_minutes")
            self._nudge_min = minutes if type(minutes) is int and minutes > 0 else 0
        if getattr(self.content, "vault_mode", False):
            # FASTT's stock vault-wide ask lane (sell_lane -> pack_sender -> content_resolver), nothing else:
            # the model never writes an offer (stock prompt_sell_catalog False) and the ask lane
            # requires the same explicit saved permission displayed by the editor.
            # Every other media writer stays off (TEXT_ONLY).
            result.update(pack_send_enabled=sales_enabled, pack_on_ask_enabled=sales_enabled, prompt_sell_catalog=False,
                          ppv_caption_1to1=False, **offer_limits(native))
            self._offer_cfg = {k: result[k] for k in ("max_open_offers", "max_offers_per_fan_per_day")}
        elif self.content:
            # Explicit pilot configuration of the existing catalogue path;
            # the vault-wide resolver and all other media writers stay off.
            result.update(prompt_sell_catalog=sales_enabled, ppv_caption_1to1=False, **offer_limits(native))
            self._offer_cfg = {k: result[k] for k in ("max_open_offers", "max_offers_per_fan_per_day")}
        if self._job == POST_BUY_KIND:
            # FASTT's own post-buy bridge, then ONE follow-up card from the vault (ai_chatter._fire_post_buy_vault_rung),
            # for this job only: the rung switch stays off for every ordinary turn.
            allowed = native.get("fastt_post_buy_upsell") is True
            result.update(enabled=allowed, post_buy_rung_enabled=allowed, fastt_vault_post_buy=allowed,
                          pack_send_enabled=allowed, pack_on_ask_enabled=allowed)
        elif self._job == DISCOUNT_KIND:
            result["enabled"] = native.get("fastt_discount_resend") is True
        if self.context:
            # Approved creator facts ride the system prompt on every turn, not
            # only when the fan asks about her (FASTT's token-saving default).
            result.update(persona_facts_on_ask_only=False)
        if await self._resumed_after_last_human():
            # An operator explicitly handed the chat back (Resume / Hand back) after the
            # last human message: FASTT's timed "cautious resume" yield is satisfied.
            result.update(resume_after_manual_hours=0)
        return result

    async def _resumed_after_last_human(self):
        """True when the newest explicit resume/release for this fan is later than the
        newest human (non-automation) outbound. Fails closed (False) on any error."""
        try:
            from sqlalchemy import select, func
            from db.engine import get_session
            from db.models import Message
            from db.onlyfans_models import OfControlAudit
            async with get_session() as session:
                resumed = (await session.execute(select(func.max(OfControlAudit.created_at)).where(
                    OfControlAudit.account_id == self.account_id, OfControlAudit.fan_id == self.fan_id,
                    OfControlAudit.action.in_(("resume_automation", "release_takeover"))))).scalar()
                human = (await session.execute(select(func.max(Message.created_at)).where(
                    Message.account_id == self.account_id, Message.fan_id == self.fan_id,
                    Message.direction == "out", Message.automation_kind.is_(None)))).scalar()
            return resumed is not None and (human is None or resumed > human)
        except Exception:
            return False

    def unanswered_replies(self, account_id):
        """Exact receipts supplied by the durable input gate, never an automation-kind exemption."""
        self._account(account_id)
        return self._unanswered_replies

    async def prompt_context(self, account_id, fan_id, *, last_inbound):
        """The single prompt hook (see ai_chatter._runtime_prompt_context)."""
        self._account(account_id)
        if type(fan_id) is not int or fan_id != self.fan_id:
            self._refuse("wrong-fan")
        if not self.context:
            return None
        try:
            return await self.context.prompt_context(account_id, fan_id, last_inbound=last_inbound)
        except Exception as exc:
            # Fixed public reasons only: never carry persona, fact or storage text.
            from .twin_context import TwinContextRefused
            self._refuse(str(exc) if isinstance(exc, TwinContextRefused) else "twin-context-unavailable")

    async def resolve_model(self, account_id, purpose, requested=None):
        self._account(account_id)
        allowed = ("ai_chatter",) + (("content_resolver",) if getattr(self.content, "vault_mode", False) else ())
        if purpose not in allowed or requested not in (None, "", self.model):
            self._refuse("forbidden-model-scope")
        return self.model

    async def chat(self, **kwargs):
        self._account(kwargs.get("account_id"))
        if (type(kwargs.get("fan_id")) is not int
                or kwargs["fan_id"] != self.fan_id
                or kwargs.get("model") != self.model):
            self._refuse("forbidden-model-scope")
        purpose = str(kwargs.get("purpose") or "")
        if purpose.startswith(RESOLVER_PURPOSE_PREFIX) or purpose == "gather_close_answer":
            # FASTT's vault ask lane (content_resolver / voice line) only, and only in vault mode.
            if not getattr(self.content, "vault_mode", False):
                self._refuse("forbidden-model-scope")
        result = await self._chat(**kwargs)
        if self.content is None or kwargs.get("response_format"):
            return result
        return await self._keep_chatting(kwargs, result)

    async def _keep_chatting(self, kwargs, result):
        """A declined offer never silences the conversation.

        FASTT validates the model's ``>>OFFER`` id against what this fan may still be offered and
        otherwise binned the price talk: a reply made of that alone was dropped, so the fan got
        nothing (cap reached) or, for a piece already pending, a second identical card. Here the
        reason is decided with FASTT's own helpers; the declined offer is recorded as an
        ``offer-declined`` runner event and the model is asked once for the plain chat reply. No
        selling rule is changed: a turn FASTT would sell on passes through untouched."""
        raw = (getattr(result, "content", None) or "")
        clean, offer_id = self.native._parse_offer_marker(raw.strip())
        if offer_id is None:
            return result
        try:
            reason = await self._offer_blocker(int(offer_id))
        except Exception:
            return result      # fail open to FASTT's own gates, never a new stop
        if reason is None:
            return result
        retried = False
        # FASTT already refuses the id on a cap or an owned item, and bins the price bubble. Two
        # things still need the wrapper: a card for a piece that is already pending (FASTT would
        # send it again) and a reply that is nothing but price talk (FASTT would drop it).
        survivors = [line for line in clean.splitlines() if line.strip() and not self.native._unbacked_talk(line)]
        if not survivors or reason in REPEAT_REASONS:
            if survivors:
                result = _with_content(result, clean)
            else:
                retry = dict(kwargs)
                retry["messages"] = list(kwargs["messages"]) + [{"role": "system", "content": NO_OFFER_NOTE}]
                result, retried = await self._chat(**retry), True
                again, _ = self.native._parse_offer_marker((getattr(result, "content", None) or "").strip())
                result = _with_content(result, again)
        self._emit({"event": "offer-declined", "reason": reason, "item_id": int(offer_id),
                    "account_id": self.account_id, "fan_id": self.fan_id, "regenerated": retried})
        return result

    async def _offer_blocker(self, offer_id):
        """Why this fan may not get a card for ``offer_id`` right now, or None. FASTT's own readers."""
        native, account, fan = self.native, self.account_id, self.fan_id
        items = await native._load_catalog(account)
        by_id = {int(i.id): i for i in items}
        target = by_id.get(offer_id)
        if target is None:
            return None          # not on the shelf: FASTT rejects the id itself
        open_offers = await native._open_offers(account, fan)
        if any(int(o.item_id) == offer_id for o in open_offers):
            return "item-already-pending"
        hero = await native._hero_media_map(account, items)
        mine = set(hero[offer_id])
        for offer in open_offers:
            if int(offer.item_id) in hero and mine & set(hero[int(offer.item_id)]):
                return "media-already-pending"
        if not await native._offerable_for_fan(account, fan, [target]):
            return "item-already-purchased"
        open_count = len(open_offers)
        if open_count and await native._unlocked_since_open_offers(account, fan, open_offers):
            open_count -= 1
        cfg = self._offer_cfg
        if open_count >= max(2, cfg["max_open_offers"]):
            return "max-open-offers"
        if not await native._offer_caps_ok(account, fan, {**cfg, "min_fan_msgs_between_offers": 0}):
            return "daily-offer-cap"
        if open_count and not await native._offer_caps_ok(account, fan, {**cfg, "min_fan_msgs_between_offers": 1}):
            return "offer-pacing"
        return None

    def make_client(self, account_id):
        self._account(account_id)
        return _DiscountClient(self) if self._job == DISCOUNT_KIND else _TextClient(self)

    async def enqueue_job(self, account_id, kind, **kwargs):
        self._account(account_id)
        if self.followups is not None:
            # Only FASTT's re-engage nudge for THIS fan is kept (runner-owned queue, never scheduled_jobs).
            job = None
            if nudge_job(kind, kwargs.get("payload"), self.fan_id):   # ai_chatter may queue only the stock nudge
                job = await asyncio.to_thread(self.followups.add, self.fan_id, kind, kwargs.get("payload"), kwargs.get("run_at"))
            if job:
                self._nudge_queued = True
                self._emit({"event": "native-followup-queued", "kind": "nudge", "fan_id": self.fan_id})
                return None
        self._emit({"event": "native-background-job-suppressed", "kind": str(kind)})
        return None

    def sent_card(self, message_id):
        """The vault card this runner sent as provider message ``message_id`` (``{"media", "previews", "price_cents"}``)
        or None. Read by ``ai_chatter._run_discount``; there is no other way to learn what a vault card held."""
        if self._card_of is None or type(message_id) is not int:
            return None
        return self._card_of(message_id)

    async def push_nickname(self, *args, **kwargs):
        return None

    async def resolve_open_offers(self, account_id, client, cfg, **kwargs):
        """FASTT's own unlock watcher / stall-TTL expiry, for THIS fan only.

        The stock body runs unchanged (``ai_chatter._resolve_open_offers_stock``); only its
        mouths are narrowed: a client that cannot send, unsend or read the platform, and
        two stock switches forced off for this call (``unsend_expired_offer``,
        ``qualification_gate_enabled``: its discount resend and ladder jobs). Writes left:
        ``content_offers`` status (expired / delivered / cancelled), ``messages.is_paid`` /
        ``purchased_at`` on a ledger-proven buy, bought-media stamps and a short fan lease."""
        self._account(account_id)
        await self._no_obligations()
        safe = dict(cfg)
        safe.update(unsend_expired_offer=False, qualification_gate_enabled=False)
        stats = await self.native._resolve_open_offers_stock(
            self.account_id, _ResolverClient(), safe,
            dry_run=bool(kwargs.get("dry_run")), only_fan_ids={self.fan_id})
        if any(stats.values()):
            self._emit({"event": "offers-resolved", **{k: v for k, v in stats.items() if v}})
        return stats

    async def bool_setting(self, account_id, name, native_loader):
        self._account(account_id)
        if name not in (
            "typing_indicator", "cat_stickers", "deliberate_typos",
            "deliberate_nonnative_style", "deliberate_nonnative_spacing",
        ):
            self._refuse("unknown-native-setting")
        return False

    async def load_day(self, account_id, *args, **kwargs):
        self._account(account_id)
        return self.native._daylog.Day()

    async def teaser_config(self, account_id, kind):
        self._account(account_id)
        if kind not in ("hot", "conversation"):
            self._refuse("unknown-teaser-kind")
        return None

    async def hold(self, account_id, fan_id, seconds, **kwargs):
        self._account(account_id)
        if type(fan_id) is not int or fan_id != self.fan_id:
            self._refuse("wrong-fan")
        # The durable Telegram action owns due-time pacing.  The native worker
        # never sleeps and later fires a stale message after restart/input change.
        return None

    def __enter__(self):
        if self._token is not None or NativeEngine._active:
            raise PilotBoundaryError("engine-already-active")
        # Lazy import: merely importing this module never loads repo .env,
        # opens a DB, constructs a platform client or resolves credentials.
        from automations import ai_chatter as native
        from telegram_runtime.capabilities import set_telegram_capabilities

        self.native = native
        NativeEngine._active = True
        try:
            self._token = set_telegram_capabilities(self)
        except BaseException:
            NativeEngine._active = False
            raise
        return self

    async def run(self, run_id, payload=None):
        """One fan turn. ``payload`` is only ever one of FASTT's own follow-up jobs for this fan (``--followups``): the
        stock nudge, or an offer-keyed discount / post-buy job the runner's post-sale scan queued."""
        if self._token is None or self._running:
            raise PilotBoundaryError("engine-not-ready")
        if type(run_id) is not int or run_id <= 0:
            raise ValueError("invalid-run-id")
        job = None
        if payload is not None:
            job = job_kind("ai_chatter", payload, self.fan_id)
            if self.followups is None or job is None:
                raise PilotBoundaryError("forbidden-job-payload")
        self._job = job
        self._running = True
        self._violation = None
        self._card_sent = self._nudge_queued = False
        try:
            await self._no_obligations()
            result = await self.native.run(self.account_id, payload if payload is not None else {
                "only_fan_ids": [self.fan_id], "max_replies": 1,
                "mode": "always", "model": self.model,
            }, run_id=run_id)
            # ai_chatter catches per-fan exceptions. A boundary refusal must
            # still reach the runner so it cannot turn into an automatic retry.
            if self._violation:
                raise PilotBoundaryError(self._violation)
            if payload is None:
                await self._queue_card_nudge()
            return result
        finally:
            self._running = False
            self._job = None

    async def _queue_card_nudge(self):
        """``--vault-mode`` + ``--followups``: the stock re-engage nudge for a card the ask lane just sent.

        FASTT only schedules its nudge on the model's ``>>OFFER`` path (ai_chatter, after ``_record_offer``);
        a vault card goes ``sell_lane -> pack_sender`` and never reaches that line. So after a turn that put
        a priced card on the wire, the same job is enqueued (same payload, same ``nudge_after_minutes``, same
        ``enqueue_job`` gate); FASTT's own ``_run_nudge`` then handles it, and the service drops it if the offer
        is settled or the fan spoke first. Best effort, like the stock schedule; never twice for one turn."""
        if (self.followups is None or not getattr(self.content, "vault_mode", False) or not self._card_sent
                or self._nudge_queued or self._nudge_min <= 0):
            return
        try:
            await self.enqueue_job(self.account_id, "ai_chatter", payload={"nudge_fan_ids": [self.fan_id]},
                                   run_at=self.followups.clock() + timedelta(minutes=self._nudge_min))
        except Exception:
            self._emit({"event": "native-followup-queue-failed", "fan_id": self.fan_id})

    def __exit__(self, exc_type, exc, tb):
        if self._token is not None:
            from telegram_runtime.capabilities import reset_telegram_capabilities
            token, self._token = self._token, None
            reset_telegram_capabilities(token)
            NativeEngine._active = False
        return False
