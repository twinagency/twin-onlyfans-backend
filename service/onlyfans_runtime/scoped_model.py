"""Account-scoped model gateway for the FASTT runner's `llm_client.chat` seam.

FASTT's ai_chatter builds its own prompts and calls `llm_client.chat`. In the
runner process that call is routed here instead of raising in the dedicated
role. The gateway holds exactly one account's sealed model credential and pinned
model; it has no house-key, tenant-key or alternate-model fallback.

Budget accounting reuses the native ledger: a conservative reservation in
GrokDailyCost/GrokCall under the account's explicit daily cap BEFORE the call,
settlement from provider usage afterwards (NativeProviderModel.settle). One HTTPS
attempt, a hard deadline, bounded output tokens, and no automatic retry:
uncertainty keeps its reservation (status 'unknown') and surfaces as an error.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import AccountAiConfig, GrokCall, GrokDailyCost
from db.onlyfans_models import OfRuntime
from llm_client import (LLMCapExceeded, LLMConfigError, LLMHTTPError, LLMResult, _clean_content,
                        _resolve_effort)
from .native_context import canonical
from .provider_model import NativeProviderModel, _decode, _units

DEFAULT_MAX_CALLS = 80
DEFAULT_DEADLINE_SECONDS = 45
DEFAULT_MAX_OUTPUT_TOKENS = 400


class ScopedModelGateway:
    def __init__(self, store, credential: NativeProviderModel, *, max_calls=DEFAULT_MAX_CALLS,
                 deadline_seconds=DEFAULT_DEADLINE_SECONDS, max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS):
        if type(credential) is not NativeProviderModel:
            raise LLMConfigError("a sealed account model credential is required")
        if (type(max_calls) is not int or not 1 <= max_calls <= 10000
                or not 1 <= deadline_seconds <= 120 or type(max_output_tokens) is not int
                or not 16 <= max_output_tokens <= 4096):
            raise LLMConfigError("invalid scoped model bounds")
        self.store, self.credential = store, credential
        self.account_id, self.model_id = credential.account_id, credential.model_id
        self.max_calls, self.deadline = max_calls, deadline_seconds
        self.max_output_tokens = max_output_tokens
        self.calls = self.failures = 0
        self._lock = asyncio.Lock()

    # -- accounting (sync, run in a worker thread; mirrors NativeProviderModel.reserve) -----------
    def _reserve(self, *, purpose, fan_id, messages, body_bytes, temperature, now):
        cred = self.credential
        with Session(self.store.engine) as s, s.begin():
            runtime = s.scalar(select(OfRuntime).where(OfRuntime.account_id == self.account_id).with_for_update())
            if runtime is None:
                raise LLMConfigError("native model account is not registered")
            cfg = s.scalar(select(AccountAiConfig).where(AccountAiConfig.account_id == self.account_id).with_for_update(read=True))
            cap = cfg.daily_cost_cap_cents if cfg is not None else None
            if type(cap) is not int or not 0 <= cap <= 1000000:
                raise LLMConfigError("an explicit valid native daily model cap is required")
            reserve = cred._cost(len(canonical(messages).encode()) + 4096, self.max_output_tokens)
            if not 1 <= reserve <= 100000000:
                raise LLMConfigError("invalid native model reservation")
            day = now.strftime("%Y-%m-%d")
            ledger = s.scalar(select(GrokDailyCost).where(GrokDailyCost.day == day,
                GrokDailyCost.account_id == self.account_id, GrokDailyCost.provider == cred.provider).with_for_update())
            spent = ledger.cost_cents if ledger is not None else 0
            if type(spent) is not int or spent < 0:
                raise LLMConfigError("invalid retained native model cost evidence")
            if spent + reserve > cap * 100:
                raise LLMCapExceeded(self.account_id, cap, (spent + reserve + 99) // 100)
            if ledger is None:
                s.add(GrokDailyCost(day=day, account_id=self.account_id, provider=cred.provider,
                    cost_cents=reserve, call_count=1, is_capped=False, updated_at=now))
            else:
                ledger.cost_cents += reserve
                ledger.call_count += 1
                ledger.updated_at = now
            call = GrokCall(purpose=purpose[:64], account_id=self.account_id, fan_id=fan_id,
                model=cred.model_id, provider=cred.provider, endpoint=cred.endpoint,
                prompt_json=body_bytes.decode(), status="pending", cost_cents=reserve,
                was_dry_run=False, called_at=now, temperature=temperature)
            s.add(call)
            s.flush()
            return call.id, reserve

    def _settle(self, call_id, reserved, actual, *, tokens=None):
        cred = self.credential
        cred.tokens_in, cred.tokens_out, cred.cache_hit_tokens = tokens or (None, None, None)
        with Session(self.store.engine) as s, s.begin():
            return cred.settle(s, account_id=self.account_id, call_id=call_id, reserved_millicents=reserved,
                               actual_millicents=actual, now=datetime.now(timezone.utc).replace(tzinfo=None))

    def _saved_effort(self):
        """Same policy as NativeProviderModel.configure_call: a stale saved value yields the model's pin."""
        with Session(self.store.engine) as s:
            cfg = s.get(AccountAiConfig, self.account_id)
            saved = getattr(cfg, "reasoning_effort", "") or ""
        saved = saved.strip() if type(saved) is str else ""
        return saved if saved in self.credential._model.effort_choices else ""

    # -- the llm_client.chat contract -----------------------------------------------------------------
    async def chat(self, *, model, messages, purpose, account_id, fan_id=None, response_format=None,
                   temperature=0.7, reasoning_effort=""):
        cred = self.credential
        if account_id != self.account_id:
            raise LLMConfigError("scoped model gateway refuses another account")
        if model != self.model_id:
            # The account's saved model is the only model this credential may call.
            raise LLMConfigError("scoped model gateway: requested model is not the account's pinned model")
        if type(messages) is not list or not messages:
            raise LLMConfigError("scoped model gateway: messages required")
        async with self._lock:  # one model call at a time
            if self.calls >= self.max_calls:
                raise LLMHTTPError("scoped model call budget for this runner is exhausted")
            self.calls += 1
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            effort = _resolve_effort(cred._model, reasoning_effort, stored=await asyncio.to_thread(self._saved_effort))
            body = {"model": cred._model.wire_model(), "messages": messages, "temperature": temperature,
                    "stream": False, "max_tokens": self.max_output_tokens}
            if response_format is not None:
                body["response_format"] = response_format
            body.update(cred._model.reasoning_keys())
            if effort:
                body["reasoning_effort"] = effort
            body_bytes = canonical(body).encode()
            call_id, reserved = await asyncio.to_thread(
                self._reserve, purpose=purpose, fan_id=fan_id, messages=messages,
                body_bytes=body_bytes, temperature=temperature, now=now)
            started = time.monotonic()
            try:
                raw = await asyncio.wait_for(cred._post_once(body_bytes), timeout=self.deadline)
            except asyncio.CancelledError:
                await asyncio.shield(asyncio.to_thread(self._settle, call_id, reserved, None))
                raise
            except Exception as exc:
                # Outcome unknown: the reservation stays (status unknown) and nothing retries.
                self.failures += 1
                await asyncio.to_thread(self._settle, call_id, reserved, None)
                raise LLMHTTPError("scoped model request outcome is uncertain: " + type(exc).__name__) from None
            latency_ms = int(round((time.monotonic() - started) * 1000))
            try:
                value = _decode(raw)
            except LLMConfigError:
                self.failures += 1
                await asyncio.to_thread(self._settle, call_id, reserved, None)
                raise LLMHTTPError("scoped model returned an invalid response") from None
            tokens, actual = None, None
            usage = value.get("usage")
            if type(usage) is dict:
                try:
                    incoming = _units(usage["prompt_tokens"], "prompt tokens")
                    outgoing = _units(usage["completion_tokens"], "completion tokens")
                    hit = usage.get("prompt_cache_hit_tokens", 0)
                    details = usage.get("prompt_tokens_details")
                    if type(details) is dict and "cached_tokens" in details:
                        hit = details["cached_tokens"]
                    hit = _units(hit, "cache-hit tokens")
                    if hit > incoming:
                        raise LLMConfigError("cache-hit usage exceeds input")
                    tokens, actual = (incoming, outgoing, hit), cred._cost(incoming, outgoing, hit)
                except (KeyError, LLMConfigError):
                    tokens = actual = None
            await asyncio.to_thread(self._settle, call_id, reserved, actual, tokens=tokens)
            try:
                choices = value["choices"]
                choice = choices[0]
                message = choice["message"]
                content = message["content"]
                # Truncated, tool-calling, empty or malformed output stops work: no partial reply is used.
                if (len(choices) != 1 or choice.get("finish_reason") != "stop" or message.get("role") != "assistant"
                        or message.get("tool_calls") or message.get("function_call")
                        or type(content) is not str or not content.strip() or len(content.encode()) > 20000):
                    raise ValueError()
            except (KeyError, TypeError, ValueError, IndexError):
                self.failures += 1
                raise LLMHTTPError("scoped model completion is incomplete or unsupported") from None
            cleaned = _clean_content(content)
            parsed = None
            if response_format and response_format.get("type") == "json_object":
                try:
                    parsed = json.loads(cleaned)
                except (ValueError, TypeError):
                    parsed = None
            tin, tout, hit = tokens or (None, None, None)
            return LLMResult(call_id=call_id, model=model, provider=cred.provider, content=cleaned,
                             parsed=parsed, reasoning_content=message.get("reasoning_content"),
                             tokens_in=tin, tokens_out=tout, cache_hit_tokens=hit,
                             cost_cents=actual or 0, latency_ms=latency_ms, raw=value)
