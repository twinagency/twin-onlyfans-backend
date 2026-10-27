"""Fan photo vision for the native runner: FASTT's own ``inbound_describe``, narrowed to what a runner may do.

Stock FASTT turns a fan's photo into a sentence stored on ``messages.image_desc``; ``ai_chatter`` then reads it
back into history as ``[he sent: ...]`` (and its photo-rating beats key off it). The only things this module
adds are the mouths that stock path needs inside a runner process:

* **model**: ``openrouter/qwen3-vl-30b`` through ``NativeVisionModel`` (the same bounded adapter, daily budget
  and fail-closed checks as the text model; one call per description, one attempt, no escalation),
  reached via the runtime capability seam ``inbound_describe`` already has (``cap.resolve_model`` /
  ``cap.chat``). Legacy ``llm_client.chat`` is never reached.
* **media fetch**: ``cap.make_client`` returns a GET-only view of the runner's verified client (its session and
  proxy), limited to https ``onlyfans.com`` hosts. Nothing else on that client is reachable.
* **no other capability**: every other capability method refuses.

Any failure leaves ``image_desc`` empty (the photo stays unanswered by the AI and flagged for a human).
"""
from __future__ import annotations

import time
from urllib.parse import urlsplit

from .model import ENDPOINT, NativePilotModel
from .scope import AccountScope

VISION_MODEL = "openrouter/qwen3-vl-30b"
VISION_PURPOSE = "describe_inbound_image"
DESCRIBE_MAX_S = 60.0
MAX_VISION_REQUEST_BYTES = 4 * 1024 * 1024     # up to four 1024px stills, base64


class VisionRefused(RuntimeError):
    """Fixed public reason only."""


class NativeVisionModel(NativePilotModel):
    ACCEPTS = VISION_MODEL
    API_MODEL = "qwen/qwen3-vl-30b-a3b-instruct"       # llm_models.MODELS[VISION_MODEL].api_model
    PROVIDER_ORDER = ["deepinfra"]
    REQUEST_LIMIT = MAX_VISION_REQUEST_BYTES


def _media_host_ok(url) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return (parts.scheme == "https" and parts.username is None and parts.port in (None, 443)
            and (host == "onlyfans.com" or host.endswith(".onlyfans.com")))


class _MediaHttp:
    def __init__(self, http):
        self._http = http

    def get(self, url, **kwargs):
        if not _media_host_ok(url):
            raise VisionRefused("forbidden-media-host")
        return self._http.get(url, **kwargs)


class MediaClient:
    """``download_frames`` needs exactly ``client.http.get`` and ``client.timeout_s``."""

    def __init__(self, client):
        self.http = _MediaHttp(client.http)
        self.timeout_s = getattr(client, "timeout_s", 20)


class VisionCapability:
    """Telegram-native capability snapshot for ONE description call; refuses everything but the vision model."""

    def __init__(self, scope: AccountScope, fan_id: int, model: NativeVisionModel, client):
        self.account_id, self.fan_id = scope.db_id, fan_id
        self._model, self._client = model, client

    def _refuse(self, *a, **k):
        raise VisionRefused("forbidden-vision-scope")

    async def resolve_model(self, account_id, purpose, requested=None):
        if str(account_id) != self.account_id or purpose != VISION_PURPOSE or requested not in (None, "", VISION_MODEL):
            self._refuse()
        return VISION_MODEL

    async def chat(self, **kwargs):
        if (str(kwargs.get("account_id")) != self.account_id or kwargs.get("fan_id") != self.fan_id
                or kwargs.get("model") != VISION_MODEL or kwargs.get("purpose") != VISION_PURPOSE
                or kwargs.get("response_format")):
            self._refuse()
        return await self._model.chat(**kwargs)

    def make_client(self, account_id):
        if str(account_id) != self.account_id:
            self._refuse()
        return MediaClient(self._client)

    async def _deny(self, *args, **kwargs):
        self._refuse()

    load_config = enqueue_job = push_nickname = resolve_open_offers = bool_setting = _deny
    load_day = teaser_config = hold = _deny


def make_describer(scope: AccountScope, client, *, api_key: str, emit, now=time.monotonic, transport=None):
    """``async describe(fan_id, message_id) -> str | None`` using FASTT's ``describe_inbound_message``."""

    async def describe(fan_id: int, message_id: int):
        from automations.tip_reward_config import image_describe_flags
        from inbound_describe import describe_inbound_message
        from telegram_runtime.capabilities import reset_telegram_capabilities, set_telegram_capabilities
        from .budget import DailyBudget

        enabled, seed, _scope = await image_describe_flags(scope.db_id)
        if not enabled:                     # FASTT's own per-account switch (tip_reward config)
            return None
        model = NativeVisionModel(
            api_key=api_key, account_id=scope.db_id, fan_id=fan_id, deadline=now() + DESCRIBE_MAX_S,
            should_stop=lambda: False, max_calls=1, transport=transport,
            on_event=lambda event: emit({**event, "purpose": VISION_PURPOSE}),
            budget=DailyBudget(scope, VISION_MODEL, endpoint=ENDPOINT))
        token = set_telegram_capabilities(VisionCapability(scope, fan_id, model, client))
        try:
            return await describe_inbound_message(scope.db_id, message_id, seed)
        finally:
            reset_telegram_capabilities(token)

    return describe
