"""Playbook: reply templates per scenario, with variants. Loaded from JSON so an agency can supply its own wording.

Template tokens (rendered by voice.render):
  {pet}            a pet name from the persona voice
  {e:heart} {e:cry} {e:eyes} {e:kiss} {e:laugh} {e:shy} {e:fire}   emoji, dropped when the persona uses none
  {item} {price}   offer label and price (offer templates only)
  {fact:KEY}       an approved persona fact; a template that needs a missing fact is skipped
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

REQUIRED = (
    # playbook scenarios
    "repetitive", "odd_hours", "conflicting_details", "ignored_question", "remember_past", "feelings", "jealous",
    "exclusive_attention", "delayed_reply", "real_relationship", "price_compare", "bored_content", "too_many_sales",
    "custom_request", "already_bought", "boundary_push", "other_creator", "family_comment", "past_promise", "secrecy",
    # engine actions
    "identity", "chargeback", "small_talk", "rating_offer", "item_offer", "vault_offer", "vip_only_redirect",
    "custom_vip_only", "custom_quote", "hard_no", "decline_ack", "purchase_thanks", "level_complete", "vip_welcome",
    "invite", "price_unavailable",
)

_TOKEN = re.compile(r"\{(pet|item|price|e:[a-z]+|fact:[a-z0-9_]+)\}")


class PlaybookError(ValueError):
    pass


@dataclass(frozen=True)
class Playbook:
    templates: dict          # key -> tuple[str, ...]

    def variants(self, key: str) -> tuple[str, ...]:
        return self.templates.get(key, ())


def playbook_from_dict(d: dict, *, require_all: bool = True) -> Playbook:
    if not isinstance(d, dict):
        raise PlaybookError("playbook must be an object of key -> [templates]")
    out = {}
    for key, vals in d.items():
        if key.startswith("_"):
            continue
        if isinstance(vals, str):
            vals = [vals]
        if not isinstance(vals, list) or not vals or not all(isinstance(v, str) and v.strip() for v in vals):
            raise PlaybookError(f"{key}: needs a non-empty list of strings")
        for v in vals:
            for tok in re.findall(r"\{([^}]*)\}", v):
                if not _TOKEN.fullmatch("{" + tok + "}"):
                    raise PlaybookError(f"{key}: unknown token {{{tok}}}")
        out[key] = tuple(vals)
    if require_all:
        missing = [k for k in REQUIRED if k not in out]
        if missing:
            raise PlaybookError(f"playbook missing keys: {', '.join(missing)}")
    return Playbook(out)


def load_playbook(*paths: str | Path) -> Playbook:
    """Later files override earlier ones key by key (a base playbook plus an agency/creator override)."""
    merged: dict = {}
    for p in paths:
        merged.update(json.loads(Path(p).read_text(encoding="utf-8")))
    return playbook_from_dict(merged)
