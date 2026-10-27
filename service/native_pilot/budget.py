"""The pilot's model spend, held in Twin's existing per-account daily budget.

This is the same ledger the rest of Twin uses: one ``grok_daily_cost`` row per
(UTC day, account, provider), compared against the account's saved
``account_ai_config.daily_cost_cap_cents`` (ledger units are hundredths of a
cent, hence the ``cap * 100`` comparison used by ``llm_client`` and
``onlyfans_runtime.provider_model``). Nothing here adds a second budget, a
house allowance or a separate counter, and nothing changes the cap.

Flow per model call, mirroring the native adapter's contract:

1. ``reserve`` - before any HTTP, atomically add a conservative estimate to the
   day's row (refused with ``LLMCapExceeded`` when it would pass the cap) and
   write a ``grok_calls`` audit row in status ``pending``. The audit row holds
   sizes and ids only, never prompt text.
2. ``settle`` - after a parsed completion, replace the estimate with the
   provider-reported cost (or the registry rate x reported tokens when no cost
   was reported) and mark the call ``done``.
3. ``hold`` - any outcome that cannot be proven (timeout, HTTP failure, bad
   body, callback failure) keeps the reservation and marks the call
   ``unknown``. Spend is never silently released.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP

from .scope import AccountScope

MAX_UNITS = 100_000_000
USD_TO_UNITS = Decimal(10000)   # 1 USD = 100 cents = 10_000 ledger units


class BudgetRefused(RuntimeError):
    """Fixed public reason only."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Reservation:
    call_id: int
    day: str
    provider: str
    reserved_units: int


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class DailyBudget:
    def __init__(self, scope: AccountScope, model_id: str, *, endpoint: str, clock=_utcnow) -> None:
        from llm_models import MODELS
        spec = MODELS.get(model_id)
        if spec is None:
            raise BudgetRefused("budget-model-unknown")
        self.scope, self.model_id, self.endpoint, self._clock = scope, model_id, endpoint, clock
        self.provider = spec.provider
        rates = [Decimal(str(spec.input_per_1k_cents)), Decimal(str(spec.output_per_1k_cents))]
        if any(not rate.is_finite() or rate <= 0 for rate in rates):
            raise BudgetRefused("budget-rates-unsupported")
        self._in, self._out = rates

    def _units(self, tokens_in: int, tokens_out: int) -> int:
        # tokens x (cents per 1k) / 1000 cents x 100 units/cent; ceiling keeps a
        # nonzero ledger value for sub-hundredth-cent calls.
        value = (Decimal(tokens_in) * self._in + Decimal(tokens_out) * self._out) / 10
        return int(value.to_integral_value(rounding=ROUND_CEILING))

    def estimate(self, request_bytes: int, max_output_tokens: int) -> int:
        # One token is never fewer than one byte of UTF-8 request, so bytes is a
        # safe upper bound on prompt tokens (refunded on settlement).
        return self._units(request_bytes, max_output_tokens)

    async def reserve(self, *, fan_id: int, request_bytes: int, max_output_tokens: int) -> Reservation:
        from sqlalchemy import select
        from db.engine import get_session
        from db.models import AccountAiConfig, GrokCall, GrokDailyCost
        from llm_client import LLMCapExceeded

        if type(fan_id) is not int or fan_id <= 0 or type(request_bytes) is not int or request_bytes <= 0:
            raise BudgetRefused("budget-request-invalid")
        reserve = self.estimate(request_bytes, max_output_tokens)
        if not 1 <= reserve <= MAX_UNITS:
            raise BudgetRefused("budget-reservation-invalid")
        now = self._clock()
        day = now.strftime("%Y-%m-%d")
        async with get_session() as s:
            cfg = await s.scalar(select(AccountAiConfig).where(
                AccountAiConfig.account_id == self.scope.db_id).with_for_update(read=True))
            cap = cfg.daily_cost_cap_cents if cfg is not None else None
            if type(cap) is not int or not 0 <= cap <= 1_000_000:
                raise BudgetRefused("budget-cap-unset")
            ledger = await s.scalar(select(GrokDailyCost).where(
                GrokDailyCost.day == day, GrokDailyCost.account_id == self.scope.db_id,
                GrokDailyCost.provider == self.provider).with_for_update())
            spent, calls = (ledger.cost_cents, ledger.call_count) if ledger is not None else (0, 0)
            if type(spent) is not int or spent < 0 or type(calls) is not int or calls < 0:
                raise BudgetRefused("budget-evidence-invalid")
            if spent + reserve > cap * 100:
                raise LLMCapExceeded(self.scope.db_id, cap, (spent + reserve + 99) // 100)
            if ledger is None:
                s.add(GrokDailyCost(day=day, account_id=self.scope.db_id, provider=self.provider,
                                    cost_cents=reserve, call_count=1, is_capped=False, updated_at=now))
            else:
                ledger.cost_cents += reserve
                ledger.call_count += 1
                ledger.updated_at = now
            call = GrokCall(purpose="ai_chatter", account_id=self.scope.db_id, fan_id=fan_id,
                            model=self.model_id, provider=self.provider, endpoint=self.endpoint,
                            prompt_json=json.dumps({"native_pilot": True, "request_bytes": request_bytes},
                                                   sort_keys=True),
                            status="pending", cost_cents=reserve, was_dry_run=False, called_at=now)
            s.add(call)
            await s.flush()
            return Reservation(call.id, day, self.provider, reserve)

    def actual_units(self, *, cost_usd, tokens_in, tokens_out) -> int | None:
        """Provider-reported cost when present, else registry rates x reported tokens."""
        if cost_usd is not None:
            units = int((Decimal(str(cost_usd)) * USD_TO_UNITS).quantize(Decimal(1), rounding=ROUND_HALF_UP))
            return units if 0 <= units <= MAX_UNITS else None
        if type(tokens_in) is int and type(tokens_out) is int and tokens_in >= 0 and tokens_out >= 0:
            units = self._units(tokens_in, tokens_out)
            return units if 0 <= units <= MAX_UNITS else None
        return None

    async def settle(self, reservation: Reservation, *, cost_usd, tokens_in, tokens_out) -> int | None:
        from sqlalchemy import select
        from db.engine import get_session
        from db.models import GrokCall, GrokDailyCost

        actual = self.actual_units(cost_usd=cost_usd, tokens_in=tokens_in, tokens_out=tokens_out)
        async with get_session() as s:
            call = await s.scalar(select(GrokCall).where(
                GrokCall.id == reservation.call_id, GrokCall.account_id == self.scope.db_id).with_for_update())
            if call is None or call.status != "pending" or call.cost_cents != reservation.reserved_units:
                raise BudgetRefused("budget-evidence-changed")
            if actual is None:
                call.status = "unknown"
                return None
            ledger = await s.scalar(select(GrokDailyCost).where(
                GrokDailyCost.day == reservation.day, GrokDailyCost.account_id == self.scope.db_id,
                GrokDailyCost.provider == reservation.provider).with_for_update())
            if ledger is None or ledger.cost_cents < reservation.reserved_units:
                raise BudgetRefused("budget-reservation-missing")
            ledger.cost_cents += actual - reservation.reserved_units
            ledger.updated_at = self._clock()
            call.status, call.cost_cents = "done", actual
            call.tokens_in, call.tokens_out = tokens_in, tokens_out
            return actual

    async def hold(self, reservation: Reservation) -> None:
        """Outcome unproven: keep the reservation, mark the audit row unknown."""
        from sqlalchemy import select
        from db.engine import get_session
        from db.models import GrokCall

        async with get_session() as s:
            call = await s.scalar(select(GrokCall).where(
                GrokCall.id == reservation.call_id, GrokCall.account_id == self.scope.db_id).with_for_update())
            if call is not None and call.status == "pending":
                call.status = "unknown"
