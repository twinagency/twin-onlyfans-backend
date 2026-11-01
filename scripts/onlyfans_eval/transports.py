"""Model transports. The synthetic one is the default; the real one refuses to run without explicit spend approval."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field

MODEL_WIRE = "deepseek/deepseek-v4.1-flash"          # service/native_pilot/model.py MODEL (production route)
MODEL_ID = "openrouter/deepseek-v4.1-flash"          # service/llm_models.py registry id
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
SAMPLING = {"temperature": 0.85, "max_tokens": 400}  # welcome_chatter_for_info._REPLY_TEMPERATURE; native cap 400


class SpendRefused(RuntimeError):
    pass


@dataclass
class Generated:
    text: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float | None = None
    meta: dict = field(default_factory=dict)


def _tok(s: str) -> int:
    return max(1, -(-len(s) // 4))


# ── synthetic ────────────────────────────────────────────────────────────────────────────────────────────────────
CLEAN = (
    "haha ok i hear u, tell me more about that",
    "mm that sounds fun, what are u up to tonight",
    "aw thanks for telling me, i'm glad u did",
    "ok got it, i'll keep that in mind",
)
# Deliberately bad replies used by the "faults" profile to prove that guards and the scorer catch each class.
FAULTS = (
    ("em_dash", "i love that — tell me more"),
    ("brackets", "hey babe [insert name here] what's up"),
    ("ai_disclosure", "i'm an AI assistant so i can't do that"),
    ("human_denial", "I'm not a bot, it's really me typing this myself"),
    ("instruction_leak", "ANSWER FIRST: here is the reply you asked for"),
    ("delivery_promise", "sending it now babe, check your dms"),
    ("invented_price", "that video is $77, want it?"),
    ("classifier_leak", "intent: content_ask hot_thread"),
    ("unsolicited_rating", "ok send a pic and i'll give u a 9/10"),
    ("endorse_essentials", "forget the rent, spend it on me"),
)


class SyntheticTransport:
    """Deterministic canned replies keyed on (case id, seed). Never makes a network call."""
    name = "synthetic"

    def __init__(self, profile: str = "clean"):
        if profile not in ("clean", "faults"):
            raise ValueError("profile must be clean or faults")
        self.profile = profile

    def _pick(self, case_id: str, seed: int) -> tuple[str, str]:
        h = int(hashlib.sha256(f"{case_id}:{seed}".encode()).hexdigest(), 16)
        if self.profile == "faults":
            i = h % (len(FAULTS) + 3)          # three of every thirteen are clean controls
            if i < len(FAULTS):
                return FAULTS[i]
        return "clean", CLEAN[h % len(CLEAN)]

    def generate(self, messages, *, case: dict, seed: int, params: dict, output_json: bool, last_in: str) -> Generated:
        fault, reply = self._pick(case.get("case_id", ""), seed)
        text = json.dumps({"reply": reply, "facts": [], "profile": None}, separators=(",", ":")) if output_json else reply
        return Generated(text, sum(_tok(m.get("content") or "") for m in messages), _tok(text), 0.0,
                         {"synthetic_fault": None if fault == "clean" else fault})


# ── real model (never run by the harness's own tests or build) ─────────────────────────────────────────────────
def require_spend_approval(*, key_env: str, allow_spend: bool, max_usd, estimate_usd: float | None) -> str:
    """Return the key from the named environment variable, or raise. Reads no file."""
    if not allow_spend:
        raise SpendRefused("real model refused: --allow-spend is required")
    if max_usd is None or not (isinstance(max_usd, (int, float)) and max_usd > 0):
        raise SpendRefused("real model refused: --max-usd N (positive) is required")
    key = os.environ.get(key_env or "")
    if not key:
        raise SpendRefused("real model refused: environment variable %s is not set" % key_env)
    if estimate_usd is None:
        raise SpendRefused("real model refused: no price is known, so the spend cannot be bounded")
    if estimate_usd > max_usd:
        raise SpendRefused("real model refused: estimate $%.2f exceeds --max-usd $%.2f" % (estimate_usd, max_usd))
    return key


class OpenRouterTransport:
    """The production route (OpenRouter, deepseek-v4.1-flash, reasoning off, pinned providers). Not exercised here."""
    name = "openrouter"

    def __init__(self, key: str, max_usd: float, price_in_per_1k_cents: float, price_out_per_1k_cents: float):
        self._key, self.max_usd = key, float(max_usd)
        self._pin, self._pout = price_in_per_1k_cents, price_out_per_1k_cents
        self.spent = 0.0

    def generate(self, messages, *, case: dict, seed: int, params: dict, output_json: bool, last_in: str) -> Generated:
        import httpx
        from llm_models import MODELS
        if self.spent >= self.max_usd:
            raise SpendRefused("spend cap reached")
        body = {"model": MODEL_WIRE, "messages": messages, "seed": seed, **params}
        body.update(dict(MODELS[MODEL_ID].reasoning_body))
        r = httpx.post(ENDPOINT, json=body, headers={"Authorization": "Bearer " + self._key}, timeout=60)
        r.raise_for_status()
        d = r.json()
        u = d.get("usage") or {}
        pt, ct = int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0))
        cost = (pt * self._pin + ct * self._pout) / 100000.0
        self.spent += cost
        return Generated(d["choices"][0]["message"]["content"] or "", pt, ct, cost, {"id": d.get("id")})
