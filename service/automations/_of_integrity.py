"""R3: facts, attachments, corrections and identity-claim helpers for OnlyFans text routes.

Pure (no DB, no I/O, no model). Extends, and does not replace, the existing creator-facts
projection (``onlyfans_runtime.creator_facts``), the memory pipeline (``native_memory``), the
photo description cache (``messages.image_desc``) and the identity guards (``native_pilot.identity``).

Authority order (highest first), stated once and rendered into the OnlyFans prompt:
  1. approved creator facts and explicit approved overrides
  2. confirmed fan facts and corrections inside this exact creator/fan relationship
  3. dated historical evidence with provenance
  4. model inferences: untrusted candidates, never automatic biography
"""
from __future__ import annotations

import json
import re
from datetime import datetime

# ── prompt fragments (appended on OnlyFans routes only) ───────────────────
AUTHORITY_ORDER = (
    "approved creator facts and overrides",
    "confirmed fan facts and his corrections in this chat",
    "dated historical evidence",
    "model inferences (untrusted candidates, never biography)",
)

FACTS_RULES = (
    "FACT DISCIPLINE (OnlyFans):\n"
    "- Your own age, job, city, family, relationship and body details come ONLY from the approved creator facts above. "
    "If one is not listed it is unknown: do not state or invent it, do not copy a number or detail he told you about himself, "
    "and do not say you are 'not telling' or that it is private just to avoid admitting you have nothing listed. "
    "Say plainly you would rather not get into that right now, or move to what you do know.\n"
    "- Facts about HIM (name, age, job, city) are his, never yours. A name or detail inside his message is not yours unless it is your approved fact. "
    "Do not ask again for something he already told you in this chat.\n"
    "- If he corrects you or a stored fact (name, age, what he said or did), accept it plainly and use his version from then on. "
    "Do not argue and do not refer to the wrong version again.\n"
    "- Who or what is in a picture he sends is unknown unless he or its description says so. Do not assume it shows him, and never attribute a picture's subject to you.\n"
    "- Never claim a promise, event or conversation that is not in this chat.\n"
)

IDENTITY_RULES = (
    "IDENTITY: never deny being automated, never say you are personally typing or that no one else is, "
    "never swear to being real or human, and never offer to describe or rate a picture as proof of anything.\n"
)

ATTACHMENT_RULES = (
    "ATTACHMENTS: a bracketed note shows when he sent a picture, video or voice note. Acknowledge that it arrived. "
    "Only describe what a note says was seen. If it says you cannot see it or it is still being looked at, say so honestly if it matters, "
    "never pretend you saw it, and never ask him to send it again just because you could not view it. "
    "Do not rate, score or judge a picture unless he asked for a rating AND a description of it is given. "
    "A screenshot, promotion or page link is not a request for a rating.\n"
)


def onlyfans_rules() -> str:
    """The block appended to the native OnlyFans system prompt."""
    return "\n" + FACTS_RULES + "\n" + IDENTITY_RULES + "\n" + ATTACHMENT_RULES


# ── attachments ───────────────────────────────────────────────────────────
RECEIVED, PROCESSING, DESCRIBED, UNSUPPORTED, FAILED = "received", "processing", "described", "unsupported", "failed"
ATTACHMENT_STATES = (RECEIVED, PROCESSING, DESCRIBED, UNSUPPORTED, FAILED)
# Same window native_pilot.service.DESCRIBE_WAIT_S holds a caption-less photo for; kept equal by a test.
DESCRIBE_WAIT_S = 45.0
_DESCRIBABLE = frozenset({"photo", "image", "video", "gif"})
_KINDS = {"photo": "photo", "image": "photo", "video": "video", "gif": "gif", "audio": "voice note", "voice": "voice note"}


def attachment_kind(raw_json) -> str:
    """photo/video/gif/voice note/attachment from the stored provider frame; 'attachment' when unknown."""
    try:
        raw = json.loads(raw_json) if isinstance(raw_json, str) and raw_json else (raw_json or {})
    except ValueError:
        raw = {}
    media = raw.get("media") if isinstance(raw, dict) else None
    if isinstance(media, list) and media:
        kinds = {_KINDS.get(str((m or {}).get("type", "")).lower(), "attachment") for m in media if isinstance(m, dict)}
        if len(kinds) == 1:
            return kinds.pop()
    if isinstance(raw, dict) and (raw.get("giphyId") or raw.get("gif")):
        return "gif"
    return "attachment"


def attachment_state(media_count, image_desc, kind, created_at, now) -> str | None:
    """State of ONE inbound message's media from existing columns only (media_count, image_desc, age).

    None when the message carries no media. 'failed' also covers a description that was deliberately skipped
    (budget or policy): in both cases there is no description and none is coming.
    """
    if not media_count:
        return None
    if (image_desc or "").strip():
        return DESCRIBED
    if kind not in _DESCRIBABLE and kind != "attachment":
        return UNSUPPORTED
    if created_at is None or now is None:
        return RECEIVED
    age = (now - created_at).total_seconds()
    return PROCESSING if age < DESCRIBE_WAIT_S else FAILED


def attachment_note(state, kind, desc=None, count=1) -> str:
    """The bracketed history note for one inbound attachment message (empty when none)."""
    if state is None:
        return ""
    what = kind if count <= 1 else f"{count} {kind}s"
    if state == DESCRIBED:
        # The description is machine-made evidence about the picture; it does not say who is in it.
        return f"[he sent {what}; machine description of what is visible, may be imperfect and does not say who it shows: {(desc or '').strip()[:600]}]"
    if state in (PROCESSING, RECEIVED):
        return f"[he sent {what}; it arrived but has not been looked at yet. Acknowledge it, do not describe or rate it, do not ask him to resend]"
    if state == UNSUPPORTED:
        return f"[he sent {what}; you cannot view this kind of attachment. Acknowledge that it arrived, do not describe or rate it, do not ask him to resend]"
    return f"[he sent {what}; it arrived but you could not view it. Acknowledge that it arrived, do not describe or rate it, do not ask him to resend it]"


def history_body(body, media_count, image_desc, raw_json, created_at, now) -> str:
    """The inbound text as shown to the planner: body plus the attachment note."""
    kind = attachment_kind(raw_json)
    state = attachment_state(media_count, image_desc, kind, created_at, now)
    note = attachment_note(state, kind, image_desc, media_count or 1)
    return (f"{body} {note}" if (body or "").strip() else note) if note else (body or "")


# ── ratings ───────────────────────────────────────────────────────────────
_RATING_REQUEST = re.compile(
    r"\brate\b|\bout\s+of\s+(?:10|ten)\b|\b\d{1,2}\s*/\s*10\b|\bscore\b|\bhow\s+(?:do\s+i\s+look|hot\s+am\s+i|does\s+(?:it|this|that)\s+look)\b|"
    r"\bwhat\s+do\s+you\s+think\s+(?:of|about)\s+(?:me|it|this|my)\b|\bam\s+i\s+(?:hot|cute|sexy|your\s+type)\b", re.I)
_RATING_OUT = re.compile(r"(?:^|\s)(?:10|[0-9](?:[.,][0-9])?)\s*(?:/\s*10|out\s+of\s+(?:10|ten))\b|^\s*(?:10|[0-9](?:[.,][0-9])?)\s*[.!]*\s*$|\bi'?d\s+give\s+(?:it|you|that)\s+(?:a\s+)?(?:\d|ten)", re.I | re.M)


def rating_requested(texts) -> bool:
    """True when any of his unanswered messages asks to be rated. An attachment or a link never counts."""
    return any(_RATING_REQUEST.search(t or "") for t in texts)


def looks_like_rating(reply) -> bool:
    return bool(_RATING_OUT.search(reply or ""))


def rating_allowed(request_texts, attachment_states) -> bool:
    """Ratings need a relevant request AND suitable attachment evidence (a description exists)."""
    return rating_requested(request_texts) and DESCRIBED in set(attachment_states)


# ── fan corrections (explicit evidence only) ──────────────────────────────
_NAME = r"([A-Z][a-zA-Z'\-]{1,24})"
_CORRECTIONS = (
    ("real_name", re.compile(r"\b(?:my\s+name\s+(?:is|'s)|i\s*'?m\s+called|call\s+me)\s+" + _NAME + r"\s*(?:,|\.|!|$|\s+not\b)"), 1),
    ("real_name", re.compile(r"\bnot\s+[A-Z][a-zA-Z'\-]{1,24}\s*,?\s*(?:it\s*'?s|my\s+name\s+is|i\s*'?m)\s+" + _NAME), 1),
    ("his_age", re.compile(r"\b(?:i\s*'?m|i\s+am|im)\s+(\d{2})\s*(?:,|\.|!|$|\s+not\b|\s+years?)", re.I), 1),
    ("his_age", re.compile(r"\bactually\s+(?:i\s*'?m|i\s+am|im)\s+(\d{2})\b", re.I), 1),
)


def detect_fan_corrections(text) -> list[dict]:
    """Explicit self-statements of name/age as quoted evidence, e.g. 'my name is Sam, not Josh'.

    Returns {field, value, quote} items whose quote is a substring of ``text``. Only fan-authored
    text may be passed in. Anything the model merely guessed is never produced here.
    """
    text = text or ""
    out, seen = [], set()
    for field, rx, group in _CORRECTIONS:
        for m in rx.finditer(text):
            value = m.group(group)
            if field == "his_age" and not 18 <= int(value) <= 120:
                continue
            if field in seen:
                continue
            seen.add(field)
            out.append({"field": field, "value": value, "quote": m.group(0).strip()})
    return out


def merge_corrections(memory, latest_body) -> dict:
    """Merge deterministic corrections into a parsed memory proposal; the fan's own words beat a model value."""
    facts = list(memory.get("facts") or [])
    fixed = detect_fan_corrections(latest_body)
    if not fixed:
        return memory
    by_field = {f["field"]: f for f in facts}
    for item in fixed:
        by_field[item["field"]] = item
    return {**memory, "facts": list(by_field.values())}


# ── age ───────────────────────────────────────────────────────────────────
def creator_age_line(facts) -> str | None:
    """The only age the creator may state: the verified one from the approved facts, else None (unknown)."""
    age = (facts or {}).get("age") if isinstance(facts, dict) else None
    return str(age) if age not in (None, "") else None


def register_header(texter_noun, *, onlyfans, verified_age, legacy_age) -> str:
    """The 'HOW YOU TEXT' header.

    Legacy (not ``onlyfans``): byte-identical to the old line, using ``legacy_age`` (persona_register_age).
    OnlyFans: the age appears only when the prompt itself carries a verified one (``verified_age``, read from the
    facts table including its '- Age: N' bullet); otherwise the header describes a voice, not an age.
    """
    if not onlyfans:
        return f"HOW YOU TEXT (a real {legacy_age}yo {texter_noun}, not an assistant):\n"
    if verified_age is not None:
        return f"HOW YOU TEXT (a real {verified_age}yo {texter_noun}, not an assistant):\n"
    return "HOW YOU TEXT (the approved creator's casual voice, not an assistant):\n"


# ── identity ──────────────────────────────────────────────────────────────
def unhandled_identity_ids(question_ids, handled_ids) -> set[int]:
    """Historical identity questions that have already been handed over are not triggered again."""
    return {int(i) for i in question_ids} - {int(i) for i in handled_ids}


def unsafe_identity_claim(reply) -> bool:
    """A reply that denies automation, claims the creator types, or uses a picture as proof of a human."""
    from native_pilot.identity import denies_automation, discloses_ai
    text = reply or ""
    if denies_automation(text) or discloses_ai(text):
        return True
    return bool(re.search(r"\b(?:send|show)\s+(?:me\s+)?(?:a\s+)?(?:pic|picture|photo|selfie)\b.{0,60}\b(?:prove|proof|bot\s+can'?t)", text, re.I)
                or re.search(r"\ba\s+bot\s+(?:can'?t|cannot|couldn'?t)\b", text, re.I))
