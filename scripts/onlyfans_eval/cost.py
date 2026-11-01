"""Cost estimator: cases x generations x (prompt + completion tokens) at the repo's openrouter price seed."""
from __future__ import annotations

from . import bootstrap

bootstrap()

from llm_models import MODELS                                 # noqa: E402
from .transports import MODEL_ID                              # noqa: E402

DEFAULT_COMPLETION_TOKENS = 70    # assumption: short multi-bubble replies; the hard cap is 400
DEFAULT_REPAIR_RATE = 0.10        # assumption: share of generations that trigger one extra bounded repair call


def price():
    m = MODELS.get(MODEL_ID)
    if m is None or m.input_per_1k_cents is None:
        return None
    return {"model": MODEL_ID, "in_per_1k_cents": m.input_per_1k_cents, "hit_per_1k_cents": m.input_cache_hit_per_1k_cents,
            "out_per_1k_cents": m.output_per_1k_cents,
            "source": "service/llm_models.py seed (comment: conservative full-input bound, UNVERIFIED for openrouter, cache saving not assumed)"}


def estimate(prompt_tokens: list, generations: int, *, completion_tokens=DEFAULT_COMPLETION_TOKENS,
             repair_rate=DEFAULT_REPAIR_RATE, cache_hit_rate=0.0):
    p = price()
    n_calls = len(prompt_tokens) * generations * (1 + repair_rate)
    tin = sum(prompt_tokens) * generations * (1 + repair_rate)
    tout = n_calls * completion_tokens
    out = {"cases": len(prompt_tokens), "generations": generations, "calls_incl_repairs": round(n_calls),
           "prompt_tokens_total": round(tin), "completion_tokens_total": round(tout),
           "mean_prompt_tokens": round(sum(prompt_tokens) / max(1, len(prompt_tokens))),
           "assumptions": {"completion_tokens": completion_tokens, "repair_rate": repair_rate, "cache_hit_rate": cache_hit_rate,
                           "token_count": "chars/4 approximation of the assembled prompt"}, "price": p}
    if p is None:
        out["usd"] = None
        out["note"] = "price unknown"
        return out
    in_c = tin * ((1 - cache_hit_rate) * p["in_per_1k_cents"] + cache_hit_rate * p["hit_per_1k_cents"]) / 1000
    out_c = tout * p["out_per_1k_cents"] / 1000
    out["usd_input"], out["usd_output"], out["usd"] = round(in_c / 100, 2), round(out_c / 100, 2), round((in_c + out_c) / 100, 2)
    return out
