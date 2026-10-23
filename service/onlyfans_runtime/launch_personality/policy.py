"""Offline review hints and readiness over the existing configuration contract.

This linter is deliberately conservative, not a complete semantic safety check.
An operator must review every draft; a clean result never grants approval,
credential validity, automation authority, or permission to send.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re
from types import SimpleNamespace
import unicodedata

from llm_models import MODELS
from model_selection import explicit_model


def _normal(value):
    return unicodedata.normalize("NFKC", value).replace("’", "'").lower()


def _negated(text, start):
    # Negation does not cross a sentence or an explicit reversal such as "but".
    prefix = re.split(r"[.!?;\n]|\b(?:but|however|instead)\b|\band\s+(?=(?:always|automatically|must)\b)|,\s*(?=(?:always|automatically|must)\b)", text[:start])[-1]
    words = re.findall(r"[a-z']+", prefix)[-8:]
    return bool(re.search(r"\b(?:no|not|never|avoid|without|cannot|can't|don't|won't)\b", " ".join(words)))


_RULES = (
    ("disclosure_suppression", "Imported instructions conflict with honest AI disclosure.", (
        r"\b(?:never|do not|don't|must not|cannot|can't)\b[^.!?;\n]{0,70}\b(?:admit|reveal|disclose|mention|say)\b[^.!?;\n]{0,70}\b(?:ai|bot|automated|automation)\b",
        r"\b(?:deny|hide|conceal)\b[^.!?;\n]{0,60}\b(?:ai|bot|automated|automation)\b",
        r"\b(?:100\s*%\s*human|never a bot|pretend to be human|claim to be human)\b",
    ), True),
    ("attention_payment_gate", "Attention or replies must not depend on imported payment or proof gates.", (
        r"\b(?:make|force)\b[^.!?;\n]{0,35}\b(?:fans?|him|them|users?)\b[^.!?;\n]{0,40}\b(?:wait|prove|earn|pay)\b",
        r"\b(?:only|don't|do not|never)\s+(?:give\s+)?(?:reply|respond|attention|replies|chat|talk)\b[^.!?;\n]{0,65}\b(?:pay|paid|payment|tip|prove|earned|earns)\w*\b",
        r"\b(?:ignore|withhold|delay)\b[^.!?;\n]{0,40}\b(?:non[- ]?paying|unpaid|attention|replies|responses)\b",
        r"\bwait\b[^.!?;\n]{0,35}\b(?:until|unless)\b[^.!?;\n]{0,35}\b(?:pays?|payment|tips?)\b",
    ), True),
    ("price_override", "Imported price, free-content or discount instructions cannot override saved fixed prices.", (
        r"\b(?:give|offer|send|sell|provide|make|set|apply|use)\b[^.!?;\n]{0,55}\b(?:free|discounts?|discounted|cheaper|price ladder|dynamic pric\w*)\b",
        r"\b(?:change|vary|adjust|reduce|lower|raise|increase|decrease|negotiate)\b[^.!?;\n]{0,35}\b(?:prices?|pricing|amount|cost)\b",
        r"\b(?:prices?|pricing)\b[^.!?;\n]{0,40}\b(?:based on|depending on|according to)\b",
        r"\bdiscount\s+(?:prices?|content|bundles?|offers?)\b",
    ), True),
    ("sales_cadence", "Sales cadence and offer eligibility belong to runtime rules, not imported personality.", (
        r"\b(?:offer|pitch|sell|upsell|send|push)\b[^.!?;\n]{0,55}\b(?:every|each|always)\b[^.!?;\n]{0,35}\b(?:messages?|repl(?:y|ies)|turns?|minutes?|hours?|days?)\b",
        r"\b(?:always|must|force|automatically)\s+(?:try to\s+)?(?:sell|upsell|pitch|offer|send ppv)\b",
        r"\b(?:send|offer|pitch)\b[^.!?;\n]{0,35}\b(?:after|within)\s+\d+\s+(?:messages?|turns?|replies)\b",
    ), True),
    ("external_redirect", "Imported off-platform contact or redirect instructions require separate operational review.", (
        r"\b(?:redirect|send|move|take|contact|message|dm|follow|visit|join|link)\b[^.!?;\n]{0,65}\b(?:telegram|whatsapp|snapchat|instagram|tiktok|discord|email|off[- ]platform|outside onlyfans|external links?)\b",
        r"(?:https?://|www\.)\S+",
        r"\b(?:redirect|move|send)\b[^.!?;\n]{0,45}\b(?:external|another platform|another site)\b",
    ), True),
    ("unsupported_content_promise", "Custom, live or unavailable-content promises must not be imported as personality.", (
        r"\b(?:promise|guarantee|deliver|provide|offer|send|make|create|record|arrange|agree to)\b[^.!?;\n]{0,65}\b(?:custom(?:s| content| videos?| photos?)?|live (?:calls?|streams?|chats?)|video calls?|unavailable content|new content on request|content not in (?:the )?catalog(?:ue)?)\b",
        r"\b(?:custom content|custom videos?|live calls?|video calls?)\b[^.!?;\n]{0,35}\b(?:available|guaranteed|on request|anytime)\b",
    ), True),
    ("prompt_override", "Imported text attempts to override the prompt or runtime instruction hierarchy.", (
        r"\b(?:ignore|override|bypass|disregard|replace)\b[^.!?;\n]{0,50}\b(?:previous|system|developer|runtime|safety|automation|sales|fixed[- ]price|all)\b[^.!?;\n]{0,30}\b(?:instructions?|rules?|prompts?|gates?|checks?|polic(?:y|ies))\b",
        r"(?:<\|(?:system|assistant|developer|im_start|im_end)\|>|\[/?inst\]|</?(?:system|developer)>)",
        r"\byou are now\b[^.!?;\n]{0,40}\b(?:system|developer|unrestricted)\b",
    ), True),
)


def conflicts(persona, creator_facts=None):
    """Return stable, deduplicated review blockers without reflecting input text."""
    if type(persona) is not str or not persona.strip() or len(persona) > 4096 or "\x00" in persona:
        return [{"code": "invalid_tone", "severity": "block", "message": "Use nonempty reviewed tone text of at most 4096 characters."}]
    if creator_facts is not None and (type(creator_facts) is not dict or any(
            type(k) is not str or (v is not None and (type(v) is not str or len(v) > 240 or "\x00" in v))
            for k, v in creator_facts.items())):
        return [{"code": "invalid_creator_facts", "severity": "block", "message": "Creator facts need review before configuration."}]
    text = _normal(persona)
    if creator_facts:
        text += "\n" + "\n".join(_normal(v) for v in creator_facts.values() if v is not None)
    found = []
    for code, message, patterns, allow_negation in _RULES:
        if any(not allow_negation or not _negated(text, m.start())
               for pattern in patterns for m in re.finditer(pattern, text)):
            found.append({"code": code, "severity": "block", "message": message})
    return found


def _credential_status(config, metadata, model_id, now):
    result = {"state": "missing", "configured": False}
    if metadata is None:
        return result
    required = {"accountId", "modelId", "configured", "expiresAt"}
    if type(metadata) is not dict or set(metadata) != required:
        return {"state": "invalid", "configured": False}
    if (type(metadata["configured"]) is not bool or type(metadata["accountId"]) is not str
            or type(metadata["modelId"]) is not str or type(metadata["expiresAt"]) is not str):
        return {"state": "invalid", "configured": False}
    if not metadata["configured"]:
        return result
    try:
        expiry = datetime.fromisoformat(metadata["expiresAt"].replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            raise ValueError()
    except (ValueError, TypeError, OverflowError):
        return {"state": "invalid", "configured": False}
    state = ("account_mismatch" if metadata["accountId"] != config.get("account_id") else
             "model_mismatch" if metadata["modelId"] != model_id else
             "expired" if expiry.astimezone(timezone.utc) <= now else "ready")
    return {"state": state, "configured": True}


def readiness(config, credential_status=None, *, now=None):
    """Report metadata readiness, without resolving keys or authorizing automation.

    ``config`` is a plain projection of AccountAiConfig columns. Credentials are
    metadata only, read by the integration's existing scoped secret owner.
    ``ready`` concerns these four checks; ``reviewRequired`` is always true.
    """
    if type(config) is not dict:
        config = {}
    now = now or datetime.now(timezone.utc)
    if type(now) is not datetime or now.tzinfo is None:
        raise ValueError("Readiness clock must include a timezone.")
    now = now.astimezone(timezone.utc)
    blockers = []
    model_id = None
    try:
        model_id = explicit_model(SimpleNamespace(model=config.get("model"),
            model_by_purpose=config.get("model_by_purpose")), "ai_chatter", MODELS)
        model = MODELS[model_id]
    except (ValueError, TypeError, RecursionError):
        model = None
    model_result = {"state": "ready" if model is not None else "missing_or_invalid",
                    "modelId": model_id, "provider": model.provider if model else None}
    if model is None:
        blockers.append("model_missing_or_invalid")
    effort = config.get("reasoning_effort")
    valid_effort = effort is None or type(effort) is str
    saved = effort.strip() if type(effort) is str else None
    resolved = saved if model and saved in model.effort_choices else model.reasoning_effort if model else None
    effort_state = ("invalid" if not valid_effort else "unavailable" if model is None else
                    "registry_default" if not saved else "ready" if saved in model.effort_choices else "registry_default_for_stale_value")
    model_result["reasoningEffort"] = {"state": effort_state, "saved": saved, "resolved": resolved}
    if not valid_effort:
        blockers.append("reasoning_effort_invalid")
    credential = _credential_status(config, credential_status, model_id, now)
    if credential["state"] != "ready":
        blockers.append("credential_" + credential["state"])
    cap = config.get("daily_cost_cap_cents")
    cap_valid = type(cap) is int and 0 <= cap <= 1000000
    budget = {"state": "missing_or_invalid" if not cap_valid else "zero" if cap == 0 else "ready",
              "dailyCostCapCents": cap if cap_valid else None}
    if budget["state"] != "ready":
        blockers.append("budget_" + budget["state"])
    tone_conflicts = conflicts(config.get("persona"))
    tone = {"state": "missing_or_invalid" if tone_conflicts and tone_conflicts[0]["code"] == "invalid_tone" else
                     "conflict" if tone_conflicts else "ready", "conflicts": tone_conflicts, "reviewRequired": True}
    if tone["state"] != "ready":
        blockers.append("tone_" + tone["state"])
    return {"ready": not blockers, "model": model_result, "credential": credential,
            "budget": budget, "tone": tone, "blockers": blockers, "reviewRequired": True}
