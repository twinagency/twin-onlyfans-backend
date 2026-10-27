"""Process-local OpenRouter transport for a single native Fastt pilot.

This does not select a persona, rewrite prompts, register a provider, persist a
key, or use Twin's conversation engine. Install ``adapter.chat`` only in the
isolated pilot process. The parent supplies the explicitly authorized key (the
account-scoped credential from the verified connection, never a house/dotenv
key), durable reservation/event callbacks and, optionally, the account's daily
budget (``budget.DailyBudget``: reserve before HTTP, settle after, hold when the
outcome is unproven). Missing cost is reported as unknown; the
legacy LLMResult integer cost field alone must not be used as a billing ledger.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable

import httpx

from .scope import AccountScope

MODEL = "deepseek/deepseek-v4.1-flash"
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
# FASTT's vault resolver JSON calls (contract read, shelf match/nearest, verify verdicts for up to
# 24 items) can need more than the default 400-token cap; a truncated reply fails closed
# ("model-output-incomplete"). Only these exact purposes get the larger bounded cap; every other
# purpose, including ai_chatter replies, stays at ``max_tokens``.
RESOLVER_MAX_TOKENS = 1200
RESOLVER_PURPOSES = frozenset({
    "content_resolver_contract", "content_resolver_match", "content_resolver_nearest",
    "content_resolver_verify"})

MAX_RESPONSE_BYTES = 128 * 1024
MAX_REQUEST_BYTES = 512 * 1024


class NativePilotModelError(RuntimeError):
    """A fixed public code, never a provider body, credential, or prompt."""


def _strict_json(value: str | bytes) -> Any:
    def reject_constant(_: str) -> None:
        raise ValueError("non-finite-json")

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict:
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate-json-key")
            result[key] = item
        return result

    return json.loads(value, parse_constant=reject_constant,
                      object_pairs_hook=object_pairs)


class NativePilotModel:
    """One request per call, exact account/fan, finite time/token/call bounds.

    ``reserve_call`` must durably reserve a call before returning if restart
    accounting is required. It takes no arguments and may raise to refuse.
    ``on_event`` receives metadata only. Callback failure stops the adapter.
    Every failure latches ``stopped_reason``; callers must not replace the
    adapter to retry an uncertain operation within the same pilot.

    The class attributes below are the whole route: a subclass (see ``photo_vision.py``) changes the
    model, provider order and request size, never the checks, budget or accounting.
    """

    ACCEPTS = MODEL              # the model name callers pass
    API_MODEL = MODEL            # what is sent, and what the response must name
    PROVIDER_ORDER = ["deepinfra/fp8", "together"]
    REQUEST_LIMIT = MAX_REQUEST_BYTES

    def __init__(
        self, *, api_key: str, account_id: str, fan_id: int, deadline: float,
        should_stop: Callable[[], bool],
        on_event: Callable[[dict[str, Any]], None],
        reserve_call: Callable[[], None] | None = None,
        max_calls: int = 80, max_tokens: int = 400,
        transport: httpx.AsyncBaseTransport | None = None,
        budget: Any = None,
    ) -> None:
        if (not isinstance(api_key, str) or not api_key
                or any(ord(ch) < 33 or ord(ch) > 126 for ch in api_key)):
            raise NativePilotModelError("model-key-invalid")
        if not AccountScope.valid(account_id):
            raise NativePilotModelError("model-account-invalid")
        if type(fan_id) is not int or fan_id <= 0:
            raise NativePilotModelError("model-fan-invalid")
        if type(max_calls) is not int or not 1 <= max_calls <= 80:
            raise NativePilotModelError("model-call-budget-invalid")
        if type(max_tokens) is not int or not 1 <= max_tokens <= 400:
            raise NativePilotModelError("model-token-budget-invalid")
        if not isinstance(deadline, (int, float)) or isinstance(deadline, bool) or not math.isfinite(deadline):
            raise NativePilotModelError("model-deadline-invalid")
        if not callable(should_stop) or not callable(on_event) or (reserve_call is not None and not callable(reserve_call)):
            raise NativePilotModelError("model-callback-invalid")
        self._api_key = api_key
        self.account_id = account_id
        self.fan_id = fan_id
        self.deadline = float(deadline)
        self._should_stop = should_stop
        self._on_event = on_event
        self._reserve_call = reserve_call
        self.max_calls = max_calls
        self.max_tokens = max_tokens
        self._transport = transport
        self._budget = budget
        self.calls = 0
        self.stopped_reason: str | None = None

    def _fail(self, code: str) -> None:
        self.stopped_reason = self.stopped_reason or code
        raise NativePilotModelError(code) from None

    def _check(self) -> None:
        if self.stopped_reason:
            self._fail(self.stopped_reason)
        try:
            stopped = self._should_stop()
        except Exception:
            self._fail("model-stop-check-failed")
        if type(stopped) is not bool:
            self._fail("model-stop-check-invalid")
        if stopped:
            self._fail("model-pilot-stopped")
        if time.monotonic() >= self.deadline:
            self._fail("model-deadline")

    async def _hold(self, reservation: Any) -> None:
        """Unproven outcome: the budget reservation is kept, never released."""
        if reservation is None or self._budget is None:
            return
        try:
            await asyncio.shield(self._budget.hold(reservation))
        except (Exception, asyncio.CancelledError):
            pass

    def _event(self, event: dict[str, Any]) -> None:
        try:
            self._on_event(event)
        except Exception:
            self._fail("model-audit-failed")

    async def chat(
        self, *, model: str, messages: list[dict], purpose: str,
        account_id: str, fan_id: int | None = None,
        response_format: dict | None = None, temperature: float = 0.7,
        reasoning_effort: str = "",
    ) -> Any:
        self._check()
        if account_id != self.account_id or type(account_id) is not str or type(fan_id) is not int or fan_id != self.fan_id:
            self._fail("model-scope-denied")
        if model != self.ACCEPTS:
            self._fail("model-route-denied")
        if reasoning_effort:
            self._fail("model-reasoning-denied")
        if not isinstance(purpose, str) or not purpose.strip() or len(purpose) > 128:
            self._fail("model-purpose-invalid")
        if not isinstance(temperature, (int, float)) or isinstance(temperature, bool) or not math.isfinite(temperature) or not 0 <= temperature <= 2:
            self._fail("model-temperature-invalid")
        # Native uses json_object. Unsupported schema modes fail before billing;
        # never silently discard a caller's requested format or schema.
        if response_format is not None and response_format not in ({"type": "json_object"}, {"type": "text"}):
            self._fail("model-format-unsupported")
        if not isinstance(messages, list) or not messages or any(not isinstance(item, dict) for item in messages):
            self._fail("model-messages-invalid")
        max_tokens = max(self.max_tokens, RESOLVER_MAX_TOKENS) if purpose in RESOLVER_PURPOSES else self.max_tokens
        body: dict[str, Any] = {
            "model": self.API_MODEL, "messages": messages, "temperature": temperature,
            "stream": False, "max_tokens": max_tokens,
            "provider": {"order": list(self.PROVIDER_ORDER), "allow_fallbacks": False},
            "reasoning": {"enabled": False}, "usage": {"include": True},
        }
        if response_format is not None:
            body["response_format"] = response_format
        try:
            encoded = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, UnicodeError, RecursionError):
            self._fail("model-request-invalid")
        if len(encoded) > self.REQUEST_LIMIT:
            self._fail("model-request-too-large")
        if self.calls >= self.max_calls:
            self._fail("model-call-budget")
        self.calls += 1
        call_number = self.calls
        try:
            if self._reserve_call is not None:
                self._reserve_call()
        except Exception:
            self._fail("model-call-reservation-refused")
        reservation = None
        if self._budget is not None:
            # Daily-budget hold BEFORE any HTTP. A refusal means no request is made.
            try:
                reservation = await self._budget.reserve(
                    fan_id=self.fan_id, request_bytes=len(encoded), max_output_tokens=max_tokens)
            except Exception as exc:
                self._fail("model-budget-exhausted" if type(exc).__name__ == "LLMCapExceeded"
                           else "model-budget-refused")
        self._event({"event": "model-start", "call": call_number,
                     "provider": "openrouter", "model": self.API_MODEL})
        started = time.monotonic()
        try:
            self._check()
            async with asyncio.timeout(min(45.0, self.deadline - time.monotonic())):
                async with httpx.AsyncClient(
                    transport=self._transport, timeout=min(45.0, self.deadline - time.monotonic()),
                    trust_env=False, follow_redirects=False,
                ) as client:
                    async with client.stream(
                        "POST", ENDPOINT, content=encoded,
                        headers={"Authorization": f"Bearer {self._api_key}",
                                 "Content-Type": "application/json", "Accept": "application/json"},
                    ) as response:
                        if response.status_code != 200:
                            raise NativePilotModelError("model-http-failed")
                        declared = response.headers.get("content-length")
                        if declared is not None and (not declared.isdecimal() or int(declared) > MAX_RESPONSE_BYTES):
                            raise NativePilotModelError("model-response-too-large")
                        raw_bytes = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(raw_bytes) + len(chunk) > MAX_RESPONSE_BYTES:
                                raise NativePilotModelError("model-response-too-large")
                            raw_bytes.extend(chunk)
            self._check()
            result, metadata = self._parse(raw_bytes, response_format, self.API_MODEL)
        except asyncio.CancelledError:
            await self._hold(reservation)
            self.stopped_reason = self.stopped_reason or "model-cancelled"
            self._event({"event": "model-failed", "call": call_number, "code": self.stopped_reason})
            raise
        except NativePilotModelError as exc:
            await self._hold(reservation)
            code = str(exc)
            self.stopped_reason = self.stopped_reason or code
            self._event({"event": "model-failed", "call": call_number, "code": code})
            raise NativePilotModelError(code) from None
        except (TimeoutError, httpx.TimeoutException):
            await self._hold(reservation)
            self.stopped_reason = "model-timeout"
            self._event({"event": "model-failed", "call": call_number, "code": self.stopped_reason})
            raise NativePilotModelError(self.stopped_reason) from None
        except Exception:
            await self._hold(reservation)
            self.stopped_reason = "model-response-failed"
            self._event({"event": "model-failed", "call": call_number, "code": self.stopped_reason})
            raise NativePilotModelError(self.stopped_reason) from None
        if reservation is not None:
            try:
                await self._budget.settle(reservation, cost_usd=metadata.get("cost_usd"),
                                          tokens_in=metadata.get("prompt_tokens"),
                                          tokens_out=metadata.get("completion_tokens"))
            except Exception:
                # Spend that cannot be recorded stops the pilot; the reservation stays.
                await self._hold(reservation)
                self.stopped_reason = self.stopped_reason or "model-budget-settle-failed"
                self._event({"event": "model-failed", "call": call_number, "code": self.stopped_reason})
                raise NativePilotModelError(self.stopped_reason) from None
        latency_ms = max(0, round((time.monotonic() - started) * 1000))
        result.latency_ms = latency_ms
        self._event({"event": "model-complete", "call": call_number,
                     "provider": "openrouter", "model": self.API_MODEL,
                     "latency_ms": latency_ms, **metadata})
        return result

    @staticmethod
    def _parse(raw_bytes: bytes | bytearray, response_format: dict | None,
               expected: str = MODEL) -> tuple[Any, dict[str, Any]]:
        # Import lazily: importing this adapter alone does not load dotenv/DB.
        from llm_client import LLMResult

        data = _strict_json(raw_bytes)
        if not isinstance(data, dict) or "error" in data or data.get("model") != expected:
            raise NativePilotModelError("model-envelope-invalid")
        choices = data.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise NativePilotModelError("model-choice-invalid")
        choice = choices[0]
        if choice.get("finish_reason") != "stop":
            raise NativePilotModelError("model-output-incomplete")
        message = choice.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant" or message.get("tool_calls") or message.get("refusal"):
            raise NativePilotModelError("model-message-invalid")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise NativePilotModelError("model-output-empty")
        parsed = None
        if response_format == {"type": "json_object"}:
            try:
                parsed = _strict_json(content)
            except (ValueError, TypeError, RecursionError):
                raise NativePilotModelError("model-json-invalid") from None
            if not isinstance(parsed, dict):
                raise NativePilotModelError("model-json-invalid")
        usage = data.get("usage", {})
        if not isinstance(usage, dict):
            raise NativePilotModelError("model-usage-invalid")
        tokens = {}
        for key in ("prompt_tokens", "completion_tokens"):
            value = usage.get(key)
            if value is not None and (type(value) is not int or value < 0):
                raise NativePilotModelError("model-usage-invalid")
            tokens[key] = value
        cached = usage.get("prompt_tokens_details", {})
        if cached is None:
            cached = {}
        if not isinstance(cached, dict):
            raise NativePilotModelError("model-usage-invalid")
        cache_hit = cached.get("cached_tokens")
        if cache_hit is not None and (type(cache_hit) is not int or cache_hit < 0):
            raise NativePilotModelError("model-usage-invalid")
        cost = usage.get("cost")
        if cost is not None and (type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0):
            raise NativePilotModelError("model-cost-invalid")
        cost_mc = int((Decimal(str(cost)) * 10000).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) if cost is not None else 0
        # The native result's raw field is deliberately only safe usage metadata.
        # No raw response or reasoning is needed by this bounded text pilot.
        result = LLMResult(
            call_id=None, model=expected, provider="openrouter", content=content,
            parsed=parsed, reasoning_content=None,
            tokens_in=tokens["prompt_tokens"], tokens_out=tokens["completion_tokens"],
            cache_hit_tokens=cache_hit, cost_cents=cost_mc, latency_ms=0,
            raw={"usage": {**tokens, "cost": cost}, "pilot_cost_known": cost is not None},
        )
        return result, {**tokens, "cost_usd": cost, "cost_known": cost is not None}
