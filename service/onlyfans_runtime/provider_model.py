"""One-attempt native model adapter using the existing model/provider registry.

Credentials are supplied explicitly for the selected account. No environment,
house-key, tenant-key or alternate-model fallback is consulted. The planner
commits invocation and cost evidence before calling generate; uncertainty keeps
the reservation and never causes a retry. No model/provider defaults change.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
import os

import httpx
from sqlalchemy import func, select

from db.models import AccountAiConfig, GrokCall, GrokDailyCost
from db.onlyfans_models import OfRuntime
from db.onlyfans_delivery_models import OfNativePlan
from sqlalchemy.orm import Session
from llm_client import LLMCapExceeded, LLMConfigError, _resolve, _resolve_effort
from .native_context import canonical
from .provider_connector import _deadline, _header
from .store import Refused

MAX_MODEL_RESPONSE_BYTES = 131072
MAX_OUTPUT_TOKENS = 4096


def _units(value, name):
    if type(value) is not int or not 0 <= value <= 10000000:
        raise LLMConfigError("invalid native model " + name)
    return value


def _decode(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise LLMConfigError("duplicate native model response key")
            value[key] = item
        return value
    def nonfinite(_):
        raise LLMConfigError("nonfinite native model response value")
    try:
        result = json.loads(raw.decode(), object_pairs_hook=unique, parse_constant=nonfinite)
    except (UnicodeError, ValueError, RecursionError):
        raise LLMConfigError("invalid native model JSON response") from None
    if type(result) is not dict:
        raise LLMConfigError("invalid native model response object")
    return result


class NativeProviderModel:
    """Explicit native model credential; construction and preparation do no I/O.

    The API body retains the saved model's wire name, reasoning mode/effort and
    existing JSON output contract. A bounded max_tokens prevents an unbounded
    completion. Registry prices are the existing accounting rates, not a claim
    that a provider's current invoice has been independently verified.
    """
    def __init__(self, *, account_id, model_id, api_key, expires_at):
        from onlyfans_read.contract import ID_PATTERN
        if type(account_id) is not str or not ID_PATTERN.fullmatch(account_id):
            raise Refused("an explicit native model account is required")
        if type(model_id) is not str:
            raise Refused("an explicit native model is required")
        model, provider = _resolve(model_id)
        self.account_id, self.model_id = account_id, model_id
        self.provider, self.endpoint = provider.name, provider.base_url.rstrip("/") + "/chat/completions"
        parsed = httpx.URL(self.endpoint)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise Refused("native model registry endpoint must be HTTPS")
        self._api_key = _header(api_key, "model credential", 4096)
        self._expires_at = _deadline(expires_at)
        self._model, self._body, self._prompt_hash = model, None, None
        self._engine, self._call_id = None, None
        self._invoked = False
        self.completed, self.reported_cost_millicents = False, None
        self.tokens_in = self.tokens_out = self.cache_hit_tokens = None
        self.sales_enabled = False

    def __repr__(self):
        return f"NativeProviderModel(account_id={self.account_id!r}, model_id={self.model_id!r})"

    def _ready(self):
        if os.environ.get("ONLYFANS_PROCESS_ROLE") != "worker":
            raise Refused("native model calls require the worker role")
        if datetime.now(timezone.utc) >= self._expires_at:
            raise Refused("native model credentials expired")

    def configure_call(self, *, account_id, chosen_model, config, messages):
        self._ready()
        if account_id != self.account_id or chosen_model != self.model_id:
            raise LLMConfigError("native credential does not match the saved account model")
        if type(config) is not dict or type(messages) is not list:
            raise LLMConfigError("native model configuration is unavailable")
        raw = canonical(messages).encode()
        if not 0 < len(raw) <= 81920:
            raise LLMConfigError("native prompt exceeds its byte budget")
        saved = config.get("reasoning_effort") or ""
        if type(saved) is not str:
            raise LLMConfigError("invalid saved native reasoning effort")
        # Match existing _account_effort policy: stale saved values yield the
        # model's explicit registry default, never another model/provider.
        saved = saved.strip()
        if saved not in self._model.effort_choices:
            saved = ""
        effort = _resolve_effort(self._model, "", stored=saved)
        body = dict(model=self._model.wire_model(), messages=messages, temperature=.7,
                    stream=False, response_format={"type": "json_object"}, max_tokens=MAX_OUTPUT_TOKENS)
        body.update(self._model.reasoning_keys())
        if effort:
            body["reasoning_effort"] = effort
        self._body = canonical(body).encode()
        self._prompt_hash = hashlib.sha256(raw).hexdigest()
        self._engine, self._call_id = None, None
        self._invoked = False
        self.completed, self.reported_cost_millicents = False, None
        self.tokens_in = self.tokens_out = self.cache_hit_tokens = None

    def _cost(self, tokens_in, tokens_out, hit=0):
        rates = [Decimal(str(self._model.input_per_1k_cents)),
                 Decimal(str(self._model.output_per_1k_cents)),
                 Decimal(str(self._model.input_cache_hit_per_1k_cents))]
        if any(not rate.is_finite() or rate < 0 for rate in rates) or min(rates[:2]) <= 0:
            raise LLMConfigError("native model accounting rates are unsupported")
        # Ceiling maintains a nonzero ledger value for sub-millicent calls.
        value = ((tokens_in-hit)*rates[0] + tokens_out*rates[1] + hit*rates[2]) / 10
        return int(value.to_integral_value(rounding=ROUND_CEILING))

    def reserve(self, session, *, account_id, fan_id, model, messages, now):
        self._ready()
        if (account_id != self.account_id or model != self.model_id or type(fan_id) is not int or fan_id <= 0
                or session.get_bind().dialect.name != "postgresql" or type(now) is not datetime
                or self._body is None or self._invoked
                or hashlib.sha256(canonical(messages).encode()).hexdigest() != self._prompt_hash):
            raise LLMConfigError("invalid scoped native model reservation")
        runtime = session.scalar(select(OfRuntime).where(OfRuntime.account_id == account_id).with_for_update())
        if runtime is None:
            raise LLMConfigError("native model account is not registered")
        now = now.astimezone(timezone.utc).replace(tzinfo=None) if now.tzinfo is not None else now
        cfg = session.scalar(select(AccountAiConfig).where(AccountAiConfig.account_id == account_id).with_for_update(read=True))
        cap = cfg.daily_cost_cap_cents if cfg is not None else None
        if type(cap) is not int or not 0 <= cap <= 1000000:
            raise LLMConfigError("an explicit valid native daily model cap is required")
        # This is a conservative byte-based estimate plus protocol overhead.
        # Actual provider usage settles the ledger, including over-estimates;
        # missing usage retains the reservation and holds the native plan.
        reserve = self._cost(len(canonical(messages).encode()) + 4096, MAX_OUTPUT_TOKENS)
        if not 1 <= reserve <= 100000000:
            raise LLMConfigError("invalid native model reservation")
        day = now.strftime("%Y-%m-%d")
        ledger = session.scalar(select(GrokDailyCost).where(GrokDailyCost.day == day,
            GrokDailyCost.account_id == account_id, GrokDailyCost.provider == self.provider).with_for_update())
        spent, calls = (ledger.cost_cents, ledger.call_count) if ledger is not None else (0, 0)
        if type(spent) is not int or spent < 0 or type(calls) is not int or calls < 0:
            raise LLMConfigError("invalid retained native model cost evidence")
        if spent + reserve > cap * 100:
            raise LLMCapExceeded(account_id, cap, (spent + reserve + 99)//100)
        if ledger is None:
            session.add(GrokDailyCost(day=day, account_id=account_id, provider=self.provider,
                cost_cents=reserve, call_count=1, is_capped=False, updated_at=now))
        else:
            ledger.cost_cents += reserve
            ledger.call_count += 1
            ledger.updated_at = now
        call = GrokCall(purpose="ai_chatter", account_id=account_id, fan_id=fan_id,
            model=model, provider=self.provider, endpoint=self.endpoint,
            prompt_json=self._body.decode(), status="pending", cost_cents=reserve,
            was_dry_run=False, called_at=now, temperature=.7)
        session.add(call)
        session.flush()
        self._engine, self._call_id = session.get_bind(), call.id
        return call, reserve, day

    def settle(self, session, *, account_id, call_id, reserved_millicents, actual_millicents, now):
        # Historical evidence must settle even if the credential expires while
        # the provider is running. This method never authorizes another call.
        if (os.environ.get("ONLYFANS_PROCESS_ROLE") != "worker" or account_id != self.account_id
                or type(call_id) is not int or call_id <= 0 or type(reserved_millicents) is not int
                or not 1 <= reserved_millicents <= 100000000 or type(now) is not datetime):
            raise LLMConfigError("invalid scoped native model settlement")
        session.scalar(select(OfRuntime.account_id).where(OfRuntime.account_id == account_id).with_for_update())
        call = session.scalar(select(GrokCall).where(GrokCall.id == call_id, GrokCall.account_id == account_id).with_for_update())
        if (call is None or call.endpoint != self.endpoint or call.was_dry_run
                or call.provider != self.provider or call.model != self.model_id):
            raise LLMConfigError("native model call evidence is missing")
        if call.status != "pending":
            return call.cost_cents if call.status == "done" else None
        if call.cost_cents != reserved_millicents:
            raise LLMConfigError("native model reservation evidence changed")
        if actual_millicents is None:
            call.status = "unknown"
            return None
        if type(actual_millicents) is not int or not 0 <= actual_millicents <= 100000000:
            raise LLMConfigError("invalid native usage evidence")
        ledger = session.scalar(select(GrokDailyCost).where(GrokDailyCost.day == call.called_at.strftime("%Y-%m-%d"),
            GrokDailyCost.account_id == account_id, GrokDailyCost.provider == call.provider).with_for_update())
        if ledger is None or ledger.cost_cents < reserved_millicents:
            raise LLMConfigError("native model retained reservation is missing")
        ledger.cost_cents += actual_millicents - reserved_millicents
        ledger.updated_at = now
        call.status, call.cost_cents = "done", actual_millicents
        call.tokens_in, call.tokens_out, call.cache_hit_tokens = self.tokens_in, self.tokens_out, self.cache_hit_tokens
        return actual_millicents

    async def generate(self, messages):
        self._ready()
        if (self._body is None or self._invoked
                or hashlib.sha256(canonical(messages).encode()).hexdigest() != self._prompt_hash):
            raise LLMConfigError("native model invocation was not prepared or was already attempted")
        if self._engine is None or self._call_id is None:
            raise LLMConfigError("native model invocation has no committed reservation")
        # Observe through a separate session after the caller's commit. The
        # ledger row must be owned by this exact durable native plan; calling
        # configure_call/generate directly cannot spend a credential.
        with Session(self._engine) as session:
            now = session.scalar(select(func.clock_timestamp())).astimezone(timezone.utc).replace(tzinfo=None)
            call = session.get(GrokCall, self._call_id)
            plan = session.scalar(select(OfNativePlan).where(
                OfNativePlan.account_id == self.account_id, OfNativePlan.model_call_id == self._call_id))
            if (call is None or call.account_id != self.account_id or call.status != "pending"
                    or call.was_dry_run or call.endpoint != self.endpoint or call.model != self.model_id
                    or plan is None or plan.status != "generating" or plan.resolved_model_id != self.model_id
                    or plan.expires_at <= now or call.fan_id != plan.fan_id):
                raise LLMConfigError("native model invocation lacks committed scoped plan evidence")
        self._invoked = True
        raw = await self._post_once(self._body)
        try:
            value = _decode(raw)
        except LLMConfigError:
            raise TimeoutError("native model completion outcome is uncertain") from None
        # Usage can be recorded even when the draft itself is refused later.
        self.completed = True
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
                    raise LLMConfigError("native cache-hit usage exceeds input")
                self.tokens_in, self.tokens_out, self.cache_hit_tokens = incoming, outgoing, hit
                self.reported_cost_millicents = self._cost(incoming, outgoing, hit)
            except (KeyError, LLMConfigError):
                self.reported_cost_millicents = None
        try:
            choices = value["choices"]
            if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
                raise ValueError()
            choice = choices[0]
            message = choice["message"]
            if (choice.get("finish_reason") != "stop" or type(message) is not dict
                    or message.get("role") != "assistant" or message.get("tool_calls")
                    or message.get("function_call")):
                raise ValueError()
            content = message["content"]
            if type(content) is not str or not content.strip() or len(content.encode()) > 20000:
                raise ValueError()
            return content
        except (KeyError, TypeError, ValueError):
            raise LLMConfigError("native model completion is incomplete or unsupported") from None

    async def _post_once(self, body):
        self._ready()
        headers = {"Authorization": "Bearer " + self._api_key, "Content-Type": "application/json",
                   "Accept": "application/json", "Accept-Encoding": "identity"}
        limits = httpx.Limits(max_connections=1, max_keepalive_connections=0)
        # The planner supplies the absolute asyncio timeout for this whole
        # operation; HTTP phase timeouts do not extend that deadline.
        transport = httpx.AsyncHTTPTransport(retries=0, trust_env=False, limits=limits)
        try:
            async with httpx.AsyncClient(transport=transport, trust_env=False, auth=None,
                    timeout=15, follow_redirects=False, limits=limits) as client:
                async with client.stream("POST", self.endpoint, content=body, headers=headers,
                                         follow_redirects=False) as response:
                    if response.status_code != 200:
                        raise TimeoutError("native model HTTP status is not completion evidence")
                    if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
                        raise LLMConfigError("encoded native model response is refused")
                    if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                        raise LLMConfigError("native model response must be JSON")
                    chunks, size = [], 0
                    async for chunk in response.aiter_raw():
                        size += len(chunk)
                        if size > MAX_MODEL_RESPONSE_BYTES:
                            raise LLMConfigError("native model response exceeded its byte budget")
                        chunks.append(chunk)
                    return b"".join(chunks)
        except LLMConfigError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            raise TimeoutError("native model request outcome is uncertain") from None
