"""Vault-AI effective-value resolution — the ONE server-side read path.

Every consumer that needs the operator-effective value of an AI-populated
`vault_items` field (description, tags, explicitness_tier, suggested_price_cents,
story_score, tip_vault_score, suggested_caption/script, …) MUST route through
`effective_value()` here — never read the raw column or `ai_fields_json`
directly. Keeping one resolver guarantees override/lock precedence is applied
identically in the API, the review builders, and the send consumers (the plan's
"one server-side effective-value write path, not UI-side").

Precedence (VAULT_AI_PLAN §1, kept by correction #10):

    locked operator override  >  unlocked operator override  >  AI field  >  legacy/default

Both override tiers read from `operator_overrides_json`; the LOCK only matters at
WRITE time — a forced describe rerun must refresh `ai_fields_json` yet never
clobber a locked effective field. Enforce that with `is_locked()` before writing.
At READ time an override always beats the AI field, so the two override tiers
collapse to a single `operator_overrides_json` lookup here.

A blank/None AI value never wins: it is treated as absent so resolution falls
through to the legacy column or default (mirrors the describe rule "never
overwrite an existing description with a refusal/blank").
"""
from __future__ import annotations

import json
import re
from typing import Any

from jsonsafe import load_json





def _is_blank(value: Any) -> bool:
    """AI-tier emptiness: None, empty/whitespace string, or empty list/dict."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) == 0
    return False


def overrides(item: Any) -> dict:
    """Operator override map from `item.operator_overrides_json` ({} if unset)."""
    return load_json(getattr(item, "operator_overrides_json", None), {}) or {}


def ai_fields(item: Any) -> dict:
    """Raw AI output map from `item.ai_fields_json` ({} if unset)."""
    return load_json(getattr(item, "ai_fields_json", None), {}) or {}


def locked_fields(item: Any) -> set[str]:
    """Set of field names the operator has locked (AI may never overwrite)."""
    raw = load_json(getattr(item, "locked_fields_json", None), [])
    return set(raw) if isinstance(raw, (list, tuple, set)) else set()


def is_locked(item: Any, field: str) -> bool:
    """True if `field` is locked — a describe rerun MUST NOT rewrite it."""
    return field in locked_fields(item)


def effective_value(item: Any, field: str, config: Any = None, *, default: Any = None) -> Any:
    """Resolve the operator-effective value of `field` on a `vault_items` row.

    Args:
        item:   a VaultItem (or any object exposing the JSON columns + attrs).
        field:  the field name, e.g. "explicitness_tier", "suggested_price_cents".
        config: parsed `vault_ai_config_json` (dict) — searched last, under a
                top-level "defaults" map, for a config-level default.
        default: explicit fallback used when nothing else resolves.

    Returns the value chosen by the precedence documented at module top.
    An override present by KEY wins even if empty (an operator blanking a field
    is intentional); an AI value that is blank/None is skipped.
    """
    ov = overrides(item)
    if field in ov:  # covers both locked and unlocked overrides (lock ⇒ write-time only)
        return ov[field]

    ai = ai_fields(item).get(field)
    if not _is_blank(ai):
        return ai

    # Legacy/compat column of the same name (e.g. description, tags,
    # suggested_price_cents, explicitness_tier) — the pre-AI durable value.
    legacy = getattr(item, field, None)
    if not _is_blank(legacy):
        return legacy

    if isinstance(config, dict):
        cfg_default = (config.get("defaults") or {}).get(field)
        if not _is_blank(cfg_default):
            return cfg_default

    return default


def normalize_tags(v: Any) -> list[str]:
    """Accept a list OR a comma/newline-separated string → deduped, lower-cased,
    trimmed tag list (cap 12, matching the describe path)."""
    if isinstance(v, str):
        parts = re.split(r"[,\n]", v)
    elif isinstance(v, (list, tuple)):
        parts = [str(x) for x in v]
    else:
        parts = []
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        t = str(p).strip().lower()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out[:12]



def project_operator_text(field, raw, overrides, locked, columns):
    """Shared override, lock and compat projection for existing text/tag edits.

    Each caller admits its own editable fields. The value and precedence rules
    stay the same for the legacy editor and the scoped native adapter.
    """
    value=normalize_tags(raw) if field=="tags" else None if raw is None else str(raw)
    overrides[field]=value
    locked.add(field)
    columns[field]=json.dumps(value) if field=="tags" else value
    return value


def description_status(item: Any) -> str | None:
    """Report legacy empty successes honestly without rewriting verification evidence.

    Folder-only review does not reject a nonblank description. Rejected text
    and intentional human blanks need review; an accidental blank is pending.
    """
    status = getattr(item, "describe_status", None)
    if status != "described":
        return status
    if any(isinstance(v, str) and v.strip() for v in
           (effective_value(item, "description"), effective_value(item, "video_description"))):
        return status
    fields = ai_fields(item)
    receipt = fields.get("_folder_evidence_v1") if isinstance(fields, dict) else None
    rejected = isinstance(receipt, dict) and isinstance(receipt.get("rejected_analysis"), dict)
    if (rejected or getattr(item, "review_state", None) == "needs_review"
            or locked_fields(item) & {"description", "video_description"}):
        return "needs_review"
    return "pending"
