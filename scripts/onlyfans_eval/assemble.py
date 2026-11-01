"""Build the REAL assembled OnlyFans prompt for an eval case (no model call, no I/O beyond imports)."""
from __future__ import annotations

import hashlib
import json
import random
import re
from datetime import datetime, timedelta

from . import bootstrap

bootstrap()

from automations import ai_chatter as chat          # noqa: E402
from automations import _voice, _of_integrity       # noqa: E402
from automations._language import detect_bot_accusation  # noqa: E402
from db.models import Fan                           # noqa: E402

BASE_TIME = datetime(2026, 10, 30, 4, 0, 0)
LANG = {"English": "en", "Spanish": "es", "French": "fr", "Portuguese": "pt"}


def case_number(case: dict) -> int:
    m = re.search(r"(\d+)", str(case.get("case_id", "")))
    return int(m.group(1)) if m else 0


def eval_fan_id(case: dict) -> int:
    """A neutral numeric id derived from the case id. The real fan id in the case file is never used."""
    return 910_000_000 + case_number(case)


def persona_for(case: dict) -> str:
    """Approved-voice persona text: the creator style label plus the case's world_state as the ONLY fact base."""
    ws = case.get("world_state") or {}
    lines = ["Approved creator voice. Clear and friendly.", "Style: " + str(case.get("creator_style_label") or "casual")]
    cat = ws.get("catalogue")
    if cat:
        lines.append("APPROVED CATALOGUE (the only items and exact prices you may mention):")
        for item in cat:
            lines.append("- %s: %s, %s %.2f" % (item.get("id"), item.get("desc"), item.get("currency", "USD"),
                                                item.get("price_cents", 0) / 100))
    else:
        lines.append("No catalogue, price list or delivery record is available for this chat.")
    for key, val in ws.items():
        if key in ("basis", "note", "catalogue"):
            continue
        lines.append("ACCOUNT FACT %s: %s" % (key, json.dumps(val, ensure_ascii=False)))
    return "\n".join(lines)


def _history(case: dict):
    rows = [(("in" if m["role"] == "fan" else "out"), m["text"]) for m in (case.get("context") or [])]
    rows += [("in", m["text"]) for m in (case.get("turn") or []) if m.get("role") == "fan"]
    return rows


def build_prompt(case: dict, *, route: str = "worker", seed: int = 0):
    """Return (messages, meta). Raises on assembly failure (the caller records that as a finding).

    route "worker": mirrors ``onlyfans_runtime.native_planning._native_messages`` (scoped text contract, complete
    answers on). route "fastt": the ``ai_chatter.run`` call shape with Twin context and the same switch.
    """
    fid = eval_fan_id(case)
    synthetic = case.get("source") == "synthetic"
    fan = Fan(account_id="eval-account", fan_id=fid, real_name=("Alex" if synthetic else None))
    cand = chat._Cand(fid)
    hist = _history(case)
    if not hist or hist[-1][0] != "in":
        raise ValueError("case has no trailing fan message")
    for i, (direction, text) in enumerate(hist):
        stamp = BASE_TIME + timedelta(minutes=3 * i)
        cand.add_message(direction, text, 1000 + i, stamp)
        if direction == "in":
            cand.last_in_text = text
            cand.fan_msg_n += 1
    cand.last_dir = "in"
    cand.last_body = hist[-1][1]
    cand.last_in_at = BASE_TIME + timedelta(minutes=3 * (len(hist) - 1))
    # the unanswered inbound run (what the real _gather fills): trailing 'in' messages
    n = 0
    for d, _ in reversed(hist):
        if d != "in":
            break
        n += 1
    cand.in_run = [cand.msg_at[len(hist) - n + j] for j in range(n)]
    lang = LANG.get((case.get("world_state") or {}).get("expected_language"), "en")
    bot_accused = detect_bot_accusation(cand.last_in_text, lang)
    kind = chat._turn_kind(bot_accused=bot_accused, pic_desc="", content_ask=False, escalation=False, image_dare=False,
                           dare_callback=False, hot_thread=False, can_sell=False, pic_offer=False)
    persona = persona_for(case)
    random.seed(seed * 7919 + case_number(case))
    if route == "worker":
        messages, _ = chat._build_messages(persona, fan, cand, set(), history_tail=100, onlyfans_text_only=True,
                                           painful_on=False, reply_format=True, onlyfans_memory=True, profile=None,
                                           dated_memory=False, onlyfans_sales=None, bot_accused=bot_accused, kind=kind,
                                           v=_voice.blocks("her", sell_customs=False), lang=lang)
        if messages and messages[0].get("role") == "system":
            messages[0] = {**messages[0], "content": messages[0]["content"] + _of_integrity.onlyfans_rules()}
    elif route == "fastt":
        messages, _ = chat._build_messages(persona, fan, cand, set(), twin_context=True, painful_on=True,
                                           reply_format=True, bot_accused=bot_accused, kind=kind,
                                           v=_voice.blocks("her"), lang=lang)
    else:
        raise ValueError("unknown route " + route)
    meta = {"route": route, "kind": kind, "lang": lang, "bot_accused": bot_accused, "fan_id": fid,
            "unanswered": n, "history_len": len(hist)}
    return messages, meta, (cand, fan)


def prompt_hash(messages) -> str:
    return hashlib.sha256(json.dumps(messages, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def approx_tokens(messages) -> int:
    """chars/4 estimate (no tokenizer is installed; marked approximate everywhere it is reported)."""
    return -(-sum(len(m.get("content") or "") for m in messages) // 4)
