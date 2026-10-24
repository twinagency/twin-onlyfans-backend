"""Process-local, text-only adapter around Fastt's actual ai_chatter engine.

This context belongs in an isolated pilot process, never a running application
server. It does not persist enablement, import Twin, or replace Fastt's prompts,
history gathering, candidate gates, reply shaping, leases or attribution.
The runner owns fresh-input admission, transport, durable send cap and recovery.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

from .scope import AccountScope

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
    "nudge_enabled": False, "rhythm_enabled": False,  # need FASTT's background job runner; jobs are
    "post_buy_rung_enabled": False,                   # suppressed here (rhythm would "brb" and never return)
    "rhythm_return_single_bubble_s": 0,
    "profile_openers_enabled": False,  # first-contact openers: new conversations stay operator-controlled
    "cadence_enabled": False,          # the runner claims each input once; a cadence skip ("rest next
                                       # tick" in stock FASTT) would leave that message unanswered for good
}


class PilotBoundaryError(RuntimeError):
    """Fixed public reason only: never embed message text or transport details."""


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
            return self.engine._send(fan_id, text, kwargs)
        except Exception:
            # The native loop intentionally catches send exceptions. Preserve a
            # stop signal outside that loop, without logging raw transport text.
            self.engine._refuse("native-send-interrupted")

    def mark_chat_read(self, fan_id):
        if type(fan_id) is not int or fan_id != self.engine.fan_id:
            self.engine._refuse("wrong-fan")
        # Reading indicators are not required for this message-only experiment.
        return None


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
                 own_offers_only=False):
        # ``account_id`` is the DB-side id ai_chatter queries with: Twin's
        # ``of-<owner>`` or a bare decimal (see native_pilot.scope).
        if not AccountScope.valid(account_id) or type(fan_id) is not int or fan_id <= 0:
            raise ValueError("invalid-pilot-identity")
        self.account_id = account_id
        self.fan_id = fan_id
        if not isinstance(model, str) or not model.strip():
            raise ValueError("invalid-pilot-model")
        self.model = model
        self.content = content
        # Optional Twin approved-context source for the single prompt hook.
        self.context = context
        # All-fans runner: another fan's open offer is that fan's business, not this turn's obligation.
        self.own_offers_only = bool(own_offers_only)
        self._chat = chat
        self._send = send
        self._emit = emit or (lambda event: None)
        self._token = None
        self._running = False
        self._violation = None
        self._offer_cfg = {"max_open_offers": 2, "max_offers_per_fan_per_day": 2}

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
        result.update(TEXT_ONLY)
        if getattr(self.content, "vault_mode", False):
            # FASTT's stock vault-wide ask lane (sell_lane -> pack_sender -> content_resolver), nothing else:
            # the model never writes an offer (stock prompt_sell_catalog False) and the ask lane
            # is armed by exactly its two stock shelf switches. Every other media writer stays off (TEXT_ONLY).
            result.update(pack_send_enabled=True, pack_on_ask_enabled=True, prompt_sell_catalog=False,
                          ppv_caption_1to1=False, **offer_limits(native))
            self._offer_cfg = {k: result[k] for k in ("max_open_offers", "max_offers_per_fan_per_day")}
        elif self.content:
            # Explicit pilot configuration of the existing catalogue path;
            # the vault-wide resolver and all other media writers stay off.
            result.update(prompt_sell_catalog=True, ppv_caption_1to1=False, **offer_limits(native))
            self._offer_cfg = {k: result[k] for k in ("max_open_offers", "max_offers_per_fan_per_day")}
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
        return _TextClient(self)

    async def enqueue_job(self, account_id, kind, **kwargs):
        self._account(account_id)
        self._emit({"event": "native-background-job-suppressed", "kind": str(kind)})
        return None

    async def push_nickname(self, *args, **kwargs):
        return None

    async def resolve_open_offers(self, account_id, client, cfg, **kwargs):
        self._account(account_id)
        await self._no_obligations()
        return {name: 0 for name in (
            "unlocked_tip", "unlocked_ppv", "offers_expired",
            "offers_unsent", "deliveries_failed", "would_unlock",
            "discount_resends")}

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

    async def run(self, run_id):
        if self._token is None or self._running:
            raise PilotBoundaryError("engine-not-ready")
        if type(run_id) is not int or run_id <= 0:
            raise ValueError("invalid-run-id")
        self._running = True
        self._violation = None
        try:
            await self._no_obligations()
            result = await self.native.run(self.account_id, {
                "only_fan_ids": [self.fan_id], "max_replies": 1,
                "mode": "always", "model": self.model,
            }, run_id=run_id)
            # ai_chatter catches per-fan exceptions. A boundary refusal must
            # still reach the runner so it cannot turn into an automatic retry.
            if self._violation:
                raise PilotBoundaryError(self._violation)
            return result
        finally:
            self._running = False

    def __exit__(self, exc_type, exc, tb):
        if self._token is not None:
            from telegram_runtime.capabilities import reset_telegram_capabilities
            token, self._token = self._token, None
            reset_telegram_capabilities(token)
            NativeEngine._active = False
        return False
