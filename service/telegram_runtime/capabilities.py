"""Scoped capability boundary used by Telegram's native ``ai_chatter`` worker.

The native engine remains the decision owner.  This interface supplies the
channel-specific mouths it needs (configuration, model, platform and durable
timing/job operations) without constructing an OnlyFans client or replacing
the engine with a second implementation.

The binding is a ContextVar so an in-flight creator turn sees one immutable
capability snapshot.  Outside a Telegram worker it is ``None`` and every
existing OnlyFans call follows its original path byte-for-byte.
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, runtime_checkable


@runtime_checkable
class TelegramNativeCapabilities(Protocol):
    account_id: str
    fan_id: int

    async def load_config(self, account_id: str, native: Mapping[str, Any]) -> dict[str, Any]: ...
    async def resolve_model(self, account_id: str, purpose: str, requested: str | None = None) -> str: ...
    async def chat(self, **kwargs: Any) -> Any: ...
    def make_client(self, account_id: str) -> Any: ...
    async def enqueue_job(self, account_id: str, kind: str, **kwargs: Any) -> Any: ...
    async def push_nickname(self, *args: Any, **kwargs: Any) -> Any: ...
    async def resolve_open_offers(self, account_id: str, client: Any,
                                  cfg: Mapping[str, Any], **kwargs: Any) -> dict[str, int]: ...
    async def bool_setting(self, account_id: str, name: str, native_loader: Any) -> bool: ...
    async def load_day(self, account_id: str, *args: Any, **kwargs: Any) -> Any: ...
    async def teaser_config(self, account_id: str, kind: str) -> Any: ...
    async def hold(self, account_id: str, fan_id: int, seconds: float, **kwargs: Any) -> None: ...


@dataclass(frozen=True)
class PromptContext:
    """What a capability supplies at ``ai_chatter``'s single prompt hook.

    ``persona`` replaces the native persona for this fan (approved prose plus the
    approved creator facts); ``finish`` receives the composed (system, user)
    messages and returns them with any additional fan-memory evidence. A
    capability opts in by defining ``async prompt_context(account_id, fan_id, *,
    last_inbound)``; it is deliberately not part of the required protocol above,
    so capabilities without it keep the native prompt byte-for-byte.
    """
    persona: str
    finish: Callable[[list], list] | None = None
    dated_memory: bool = False


_active: contextvars.ContextVar[TelegramNativeCapabilities | None] = contextvars.ContextVar(
    "fastt_telegram_native_capabilities", default=None
)


def set_telegram_capabilities(value: TelegramNativeCapabilities) -> contextvars.Token:
    """Install one trusted worker snapshot for the current async context."""
    if not isinstance(value, TelegramNativeCapabilities):
        raise TypeError("invalid Telegram native capabilities")
    return _active.set(value)


def reset_telegram_capabilities(token: contextvars.Token) -> None:
    _active.reset(token)


def active_telegram_capabilities() -> TelegramNativeCapabilities | None:
    return _active.get()
