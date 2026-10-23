"""Offline, evidence-backed personality observations for human review.

This module never fetches provider data, calls a model, or produces runtime
instructions. Its caller supplies an already-authorized, account-bound export.
Outgoing messages demonstrate account writing, not necessarily creator authorship.
"""
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
import hashlib
import json
import re


MAX_MESSAGES = 5000
MAX_EXAMPLES = 40
MAX_INPUT_BYTES = 4 * 1024 * 1024
AUTHORS = ("creator", "agency", "unknown", "automation")
_OPERATIONAL = re.compile(
    r"\b(?:ppv|unlock|discount|bundle|tip(?:s|ping)?|subscribe|subscription|"
    r"price|pricing|buy|purchase|pay(?:ment|ing)?|free(?:bie)?|always|never|must|"
    r"ignore|instructions?|redirect|send|click|prove\s+(?:it|yourself)|make\s+\w+\s+wait)\b|"
    r"[$€£¥]\s*\d|\b\d+(?:\.\d{1,2})?\s*(?:usd|aud|eur|dollars?)\b",
    re.IGNORECASE,
)
_EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")
_IDENTIFIER = re.compile(r"[A-Za-z0-9._:/-]{1,160}\Z")


class InvalidImport(ValueError):
    """The export is malformed, exceeds the offline budget, or crosses scope."""


def _text(value, label, *, maximum, empty=False):
    try:
        size = len(value.encode("utf-8")) if type(value) is str else maximum + 1
    except UnicodeError:
        raise InvalidImport("Invalid " + label + ".") from None
    if (type(value) is not str or size > maximum
            or any(ord(char) < 32 and char not in "\n\r\t" for char in value)
            or not empty and not value.strip()):
        raise InvalidImport("Invalid " + label + ".")
    return value.strip()


def _identifier(value, label):
    if type(value) is not str or not _IDENTIFIER.fullmatch(value):
        raise InvalidImport("Invalid " + label + ".")
    return value


def _stamp(value):
    value = _text(value, "message timestamp", maximum=64)
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.utcoffset() is None:
            raise ValueError()
        return stamp.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise InvalidImport("A timezone-aware message timestamp is required.") from None


def _evidence_id(kind, source, scope):
    raw = json.dumps([kind, source, *scope], ensure_ascii=False, separators=(",", ":"))
    return "e_" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def _representative(rows):
    """Round-robin authors, then time bins and fans; deduplicate repeated copy."""
    if not rows:
        return []
    first, last = rows[0]["stamp"], rows[-1]["stamp"]
    span = (last - first).total_seconds()
    queues = {}
    for author in AUTHORS:
        groups = defaultdict(list)
        for row in rows:
            if row["author"] != author:
                continue
            position = (row["stamp"] - first).total_seconds()
            time_bin = min(3, int(position / span * 4)) if span else 0
            groups[(time_bin, row["fanKey"])].append(row)
        # Rotate time bins before fans so a prolific early fan cannot consume
        # the full selection before later periods are represented.
        bins = defaultdict(list)
        for (time_bin, fan), values in sorted(groups.items()):
            bins[time_bin].append(deque(values))
        ordered_groups = []
        while any(bins.values()):
            for time_bin in sorted(bins):
                if bins[time_bin]:
                    ordered_groups.append(bins[time_bin].pop(0))
        queue = deque()
        while any(ordered_groups):
            for group in ordered_groups:
                if group:
                    queue.append(group.popleft())
        queues[author] = queue
    selected = []
    while len(selected) < MAX_EXAMPLES and any(queues.values()):
        for author in AUTHORS:
            if queues[author] and len(selected) < MAX_EXAMPLES:
                selected.append(queues[author].popleft())
    return sorted(selected, key=lambda row: (row["stamp"], row["sourceId"]))


def _suggested_persona(metrics):
    """Controlled tone draft from measurements, never message/profile wording."""
    author = next((author for author in ("creator", "unknown", "agency") if author in metrics), None)
    result = {"text": "", "attribution": author, "evidenceIds": [], "selected": False,
              "requiresReview": True, "basis": "observed_account_style", "warning": None}
    if author is None:
        return result
    values = metrics[author]
    count = values["count"]
    lowercase, emoji, questions = (values[key] / count for key in ("lower", "emoji", "questions"))
    length = max(1, round(values["words"]))
    case_text = ("Prefer mostly lowercase replies." if lowercase >= .75 else
                 "Use lowercase-only replies sparingly." if lowercase <= .25 else
                 "Vary lowercase-only replies with capitalized replies.")
    emoji_text = ("Avoid adding emojis by default." if emoji == 0 else
                  "Use emojis sparingly." if emoji < .25 else
                  "Use emojis sometimes when suitable." if emoji < .75 else
                  "Use emojis frequently when suitable.")
    question_text = ("Do not force a question into each reply." if questions == 0 else
                     "Use questions sparingly when suitable." if questions < .25 else
                     "Use questions sometimes when suitable." if questions < .75 else
                     "Use questions frequently when suitable.")
    result["text"] = " ".join((f"Match the observed reply length of roughly {length} words when appropriate.",
                                case_text, emoji_text, question_text))
    result["evidenceIds"] = values["evidenceIds"]
    if author == "unknown":
        result["warning"] = "This draft reflects account writing with unverified authorship; creator writing style is unverified."
    elif author == "agency":
        result["warning"] = "This draft reflects agency-authored account writing; creator writing style is unverified."
    return result


def build_draft(profile, messages):
    """Validate an export and return unselected observations with provenance.

    Profile fields use the existing creator fact allowlist. Messages must be
    outgoing and explicitly scoped; unknown authorship stays unknown. Repeated
    copy is deduplicated within each authorship class, preserving provenance
    differences when the same wording was used by both agency and creator.
    """
    from automations._persona_contract import PERSONA_FACT_FIELDS

    if type(profile) is not dict or set(profile) != {
            "accountId", "bindingId", "creatorId", "sourceId", "fields"}:
        raise InvalidImport("A scoped profile export is required.")
    scope = tuple(_identifier(profile[key], key) for key in ("accountId", "bindingId", "creatorId"))
    profile_source = _identifier(profile["sourceId"], "profile source")
    fields = profile["fields"]
    allowed = dict(PERSONA_FACT_FIELDS)
    if type(fields) is not dict or not set(fields) <= set(allowed):
        raise InvalidImport("Use supported explicit creator facts only.")
    facts = {}
    for key, value in fields.items():
        value = _text(value, "creator fact", maximum=240)
        if key == "age" and (not re.fullmatch(r"[0-9]{2,3}", value) or not 18 <= int(value) <= 120):
            raise InvalidImport("Creator age must be an adult age between 18 and 120.")
        facts[key] = value
    if type(messages) is not list or len(messages) > MAX_MESSAGES:
        raise InvalidImport("The export supports at most 5000 outgoing messages.")
    try:
        size = len(json.dumps([profile, messages], ensure_ascii=False, separators=(",", ":")).encode())
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise InvalidImport("The export must contain valid JSON values.") from None
    if size > MAX_INPUT_BYTES:
        raise InvalidImport("The export exceeds its byte budget.")
    rows, seen = [], {profile_source}
    authorship = Counter({author: 0 for author in AUTHORS})
    for message in messages:
        if type(message) is not dict or set(message) != {
                "accountId", "bindingId", "sourceId", "direction", "body", "author", "fanKey", "occurredAt"}:
            raise InvalidImport("A complete outgoing-message export is required.")
        if (message["accountId"], message["bindingId"]) != scope[:2]:
            raise InvalidImport("Message scope does not match the creator profile.")
        if message["direction"] != "out" or message["author"] not in AUTHORS:
            raise InvalidImport("Only outgoing messages with explicit authorship are supported.")
        source = _identifier(message["sourceId"], "message source")
        if source in seen:
            raise InvalidImport("Duplicate source identifiers are not permitted.")
        seen.add(source)
        row = {
            "sourceId": source,
            "body": _text(message["body"], "message body", maximum=8000),
            "author": message["author"],
            "fanKey": _identifier(message["fanKey"], "sampling fan key"),
            "stamp": _stamp(message["occurredAt"]),
        }
        authorship[row["author"]] += 1
        rows.append(row)
    rows.sort(key=lambda row: (row["stamp"], row["sourceId"]))
    unique, repeated = [], set()
    for row in rows:
        key = (row["author"], " ".join(row["body"].split()))
        if key not in repeated:
            repeated.add(key)
            unique.append(row)
    sample = _representative(unique)
    evidence, creator_facts, style, instructions = [], [], [], []
    for key in allowed:
        if key not in facts:
            continue
        evidence_id = _evidence_id("profile:" + key, profile_source, scope)
        evidence.append({"id": evidence_id, "sourceId": profile_source, "kind": "profile",
                         "author": "profile", "excerpt": key + ": " + facts[key], "truncated": False})
        creator_facts.append({"key": key, "value": facts[key], "evidenceIds": [evidence_id],
                              "attribution": "profile", "selected": False})
    style_rows = defaultdict(list)
    for row in sample:
        evidence_id = _evidence_id("message", row["sourceId"], scope)
        evidence.append({"id": evidence_id, "sourceId": row["sourceId"], "kind": "outgoing_message",
                         "author": row["author"], "excerpt": row["body"][:400], "truncated": len(row["body"]) > 400})
        if _OPERATIONAL.search(row["body"]):
            instructions.append({"text": row["body"][:400], "evidenceIds": [evidence_id],
                                 "attribution": row["author"], "selected": False})
        elif row["author"] != "automation":
            style_rows[row["author"]].append((row["body"], evidence_id))
    metrics = {}
    for author in AUTHORS:
        values = style_rows[author]
        if not values:
            continue
        count = len(values)
        evidence_ids = [evidence_id for _, evidence_id in values]
        words = sum(len(body.split()) for body, _ in values) / count
        lower = sum(any(char.isalpha() for char in body) and body.lower() == body for body, _ in values)
        emoji = sum(bool(_EMOJI.search(body)) for body, _ in values)
        questions = sum("?" in body for body, _ in values)
        metrics[author] = {"count": count, "words": words, "lower": lower, "emoji": emoji,
                           "questions": questions, "evidenceIds": evidence_ids}
        observations = (
            ("message_length", f"Observed {author} sample averages {words:.1f} words per message ({count} examples)."),
            ("letter_case", f"Observed {author} sample uses lowercase-only letters in {lower}/{count} messages."),
            ("emoji", f"Observed {author} sample contains emoji in {emoji}/{count} messages."),
            ("questions", f"Observed {author} sample contains question marks in {questions}/{count} messages."),
        )
        for key, value in observations:
            style.append({"key": key, "value": value, "evidenceIds": evidence_ids,
                          "attribution": author, "selected": False})
    limitations = [
        "Outgoing account messages do not prove creator authorship; author labels are supplied by the export.",
        "Style observations are descriptive and require review; they are not runtime instructions.",
        "Creator facts come only from explicit profile fields; message claims are not promoted to facts.",
        "Sales and directive-like examples are quarantined and must not override runtime rules.",
        "Sampling is bounded and deterministic; it is not a claim of full-history coverage.",
        "Evidence excerpts are limited to 400 characters; truncated excerpts are marked explicitly.",
    ]
    if not authorship["creator"]:
        limitations.append("No creator-authored messages were supplied; creator writing style is unverified.")
    if not messages:
        limitations.append("No outgoing messages were supplied; this draft contains profile facts only.")
    return {
        "creatorFacts": creator_facts,
        "writingStyle": style,
        "operationalInstructions": instructions,
        "suggestedPersona": _suggested_persona(metrics),
        "evidence": evidence,
        "coverage": {"inputMessages": len(rows), "uniqueMessages": len(unique),
                     "sampledMessages": len(sample), "authorship": dict(authorship),
                     "method": "author-time-fan-round-robin.v1", "limitations": limitations},
    }
