"""Process-local, text-only adapter around Fastt's actual ai_chatter engine.

This context belongs in an isolated pilot process, never a running application
server. It does not persist enablement, import Twin, or replace Fastt's prompts,
history gathering, candidate gates, reply shaping, leases or attribution.
The runner owns fresh-input admission, transport, durable send cap and recovery.
"""
from __future__ import annotations

MODEL = "deepseek/deepseek-v4.1-flash"

# Independent switches are explicit: disabling the sales catalogue alone does
# not turn off Fastt's teaser, pack, sticker or scheduled follow-up paths.
TEXT_ONLY = {
    "enabled": True, "mode": "always", "max_fans_per_tick": 1,
    "payers_only": False,
    "pack_send_enabled": False, "pack_on_ask_enabled": False,
    "prompt_sell_catalog": False, "qualification_gate_enabled": False,
    "smart_pricing_enabled": False, "cadence_enabled": False,
    "nudge_enabled": False, "rhythm_enabled": False,
    "rhythm_return_single_bubble_s": 0, "pacing_enabled": False,
    "life_tip_ask_enabled": False, "life_tip_big_enabled": False,
    "post_buy_rung_enabled": False, "hook_upsell_enabled": False,
    "hook_upsell_early_ask": False, "gift_enabled": False,
    "filming_stall_enabled": False, "profile_openers_enabled": False,
    "force_ask": False, "pivot_on_escalation": False,
    "engage_old_fans": False,
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


class NativeEngine:
    """Scoped Telegram capabilities around the real native engine.

    This class used to monkeypatch twelve module globals.  It now implements the
    production capability contract consumed explicitly by ``ai_chatter``.  The
    context token is reversible and task-local; no second conversation engine
    or hidden process-global routing table exists.
    """

    _active = False

    def __init__(self, account_id, fan_id, *, chat, send, emit=None, model=MODEL, content=None):
        if (not isinstance(account_id, str) or not account_id.isdecimal()
                or type(fan_id) is not int or fan_id <= 0):
            raise ValueError("invalid-pilot-identity")
        self.account_id = account_id
        self.fan_id = fan_id
        if not isinstance(model, str) or not model.strip():
            raise ValueError("invalid-pilot-model")
        self.model = model
        self.content = content
        self._chat = chat
        self._send = send
        self._emit = emit or (lambda event: None)
        self._token = None
        self._running = False
        self._violation = None

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
        async with get_session() as session:
            row = (await session.execute(select(ContentOffer.id).where(
                ContentOffer.account_id == self.account_id,
                ContentOffer.status.in_(("open", "unlocked")),
            ).limit(1))).first()
        if row is not None:
            self._refuse("open-offer-obligation")

    async def load_config(self, account_id, native):
        self._account(account_id)
        result = dict(native)
        result.update(TEXT_ONLY)
        if self.content:
            # Explicit pilot configuration of the existing catalogue path;
            # the vault-wide resolver and all other media writers stay off.
            result.update(prompt_sell_catalog=True, ppv_caption_1to1=False,
                          min_fan_msgs_between_offers=0, max_offers_per_fan_per_day=2)
        return result

    async def resolve_model(self, account_id, purpose, requested=None):
        self._account(account_id)
        if purpose != "ai_chatter" or requested not in (None, "", self.model):
            self._refuse("forbidden-model-scope")
        return self.model

    async def chat(self, **kwargs):
        self._account(kwargs.get("account_id"))
        if (type(kwargs.get("fan_id")) is not int
                or kwargs["fan_id"] != self.fan_id
                or kwargs.get("model") != self.model):
            self._refuse("forbidden-model-scope")
        return await self._chat(**kwargs)

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
