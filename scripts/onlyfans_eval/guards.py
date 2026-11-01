"""Run the production pre-model gate and post-model guards over a raw model reply. Pure/local; no sender."""
from __future__ import annotations

import asyncio

from . import bootstrap

bootstrap()

from automations import ai_chatter as chat                      # noqa: E402
from automations import _of_integrity, _inbound_intent          # noqa: E402
from automations._outbound import finalize_draft                # noqa: E402
from automations.pack_claim import has_delivery_promise         # noqa: E402
from automations.welcome_chatter_for_info import split_for_bubbles, _looks_like_echo  # noqa: E402
from automations import _language                               # noqa: E402
from native_pilot import safety_detect                          # noqa: E402
from native_pilot.identity import discloses_ai, sincere_identity_question  # noqa: E402
from native_pilot.media_claims import echoes_instruction, claims_media_sent  # noqa: E402

ALL_SCOPE_HOLDS = (safety_detect.STOP_CONTACT, safety_detect.DISTRESS, safety_detect.CONSENT, safety_detect.IDENTITY)


def pre_gate(case: dict) -> dict:
    """What the plan-time gates do with the fan's unanswered turn BEFORE any model call.

    Mirrors native_planning.review_inbound: promo/business burst suppression (_inbound_intent) and the R1 detectors
    (safety_detect.assess + the identity question detector). Returns {"action": "reply"|"hold", "reasons": [...],
    "commercial_blocked": bool}.
    """
    texts = [m["text"] for m in case.get("turn", []) if m.get("role") == "fan"]
    reasons, commercial = [], False
    allowed, results = _inbound_intent.burst_decision(texts)
    if not allowed:
        reasons.append("promo_or_business_burst")
    verdicts = safety_detect.assess(texts)
    for v in verdicts:
        if v.category in ALL_SCOPE_HOLDS or v.scope == "all":
            reasons.append(f"{v.category}:{v.code}")
        elif v.category in (safety_detect.HARDSHIP, safety_detect.COMPLAINT):
            commercial = True
    if any(sincere_identity_question(t) for t in texts) and not any(r.startswith("identity") for r in reasons):
        reasons.append("identity:sincere_question")
    return {"action": "hold" if reasons else "reply", "reasons": reasons, "commercial_blocked": commercial,
            "intent_labels": [r.label for r in results]}


def _rating_allowed(case: dict) -> bool:
    return case.get("rubric_expectations", {}).get("key") in ("rating_ok",)


async def _fd(text, account_id, fan_id):
    return await finalize_draft(text, account_id=account_id, fan_id=fan_id, purpose="onlyfans_native_text")


def post_guards(raw_reply: str, *, case: dict, ctx, route: str, transport, messages, seed: int) -> dict:
    """Guard chain over the model's reply text. Returns verdicts + the post-guard bubbles ([] when held)."""
    cand, fan = ctx
    verdicts, held = {}, []
    text = (raw_reply or "").strip()
    text, _offer = chat._parse_offer_marker(text)
    verdicts["draft_problems"] = chat._draft_problems(text, cand, fan, "") if text else ["empty"]
    if route == "fastt" and text and verdicts["draft_problems"]:
        # production runs ONE bounded repair (ai_chatter._complete_repair) for these; exercise it with the same transport
        async def chat_stub(**kw):
            g = transport.generate(kw["messages"], case=case, seed=seed + 1, params={}, output_json=False,
                                   last_in=cand.last_body)
            return type("R", (), {"content": g.text})()
        real = chat._runtime_chat
        chat._runtime_chat = chat_stub
        try:
            fixed = asyncio.run(chat._complete_repair(text, messages, c=cand, f=fan, kind="", model=None,
                                                      account_id="eval-account", fan_id=fan.fan_id))
        finally:
            chat._runtime_chat = real
        verdicts["repair_attempted"] = True
        verdicts["draft_problems_after_repair"] = chat._draft_problems(fixed, cand, fan, "")
        text = fixed
    out, leak = asyncio.run(_fd(text, "eval-account", fan.fan_id))
    verdicts["finalize_leak"] = leak
    if leak or not out.strip():
        held.append("finalize_draft:" + (",".join(leak) if leak else "empty"))
    if chat._unbacked_talk(out):
        held.append("unbacked_price_or_specifics")
    if chat._DELIVERY_TALK_RE.search(out):
        held.append("delivery_talk")
    if has_delivery_promise(out):
        held.append("delivery_promise")
    if claims_media_sent(out):
        held.append("media_claim")
    if discloses_ai(out):
        held.append("identity_disclosure")
    if echoes_instruction(out):
        held.append("instruction_text")
    if _of_integrity.unsafe_identity_claim(out):
        held.append("identity_claim")
    if _of_integrity.looks_like_rating(out) and not _rating_allowed(case):
        held.append("unsolicited_rating")
    ov = safety_detect.output_violation(out)
    if ov:
        held.append("output_violation:" + ov)
    verdicts["held"] = held
    bubbles = []
    if not held:
        parts = [_language.apply_word_restriction(p, "en") for p in split_for_bubbles(out, 2, preserve_complete=True)]
        bubbles = [p for p in parts if p.strip() and not _looks_like_echo(p, cand.last_body)]
        if not bubbles:
            held.append("echo_or_empty_after_split")
    return {"verdicts": verdicts, "post_guard_text": "\n\n".join(bubbles), "bubbles": bubbles, "held_reasons": held,
            "finalized_text": out}
