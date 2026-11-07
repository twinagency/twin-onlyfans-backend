"""Pure bookkeeping for automatic vault descriptions (no I/O, no provider calls).

Everything lives inside the existing `ai_fields_json` blob, so no schema change
is needed:

    _content_stamp      "<hash_kind>:<content_hash>" of the media WHEN it was
                        described. A later mismatch means the content changed;
                        an expiring URL or unrelated metadata never touches it.
    _frame_source       photo | storyboard | poster  (what the model actually saw)
    _frames             number of images sent
    _sampling_note      human-readable limitation, e.g. "9 frames sampled in
                        order from a 74s clip; audio and unsampled moments unseen"
    _describe_failure   {"category", "attempts", "exhausted"} for the LAST failure

Failure categories are deliberately separate and never collapsed:
    auth         credential missing/refused  -> item NOT touched (not its fault)
    budget       spending cap / provider credit -> item NOT touched
    fetching     could not read the media bytes
    model        provider/model error or refusal
    unsupported  nothing renderable (DRM-only, poster-only video, wrong kind)
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from jsonsafe import load_json

CATEGORIES = ("auth", "budget", "fetching", "model", "unsupported")
MAX_ATTEMPTS = 5                 # per content version; then explicitly unresolved
BASE_RETRY = timedelta(minutes=30)   # SQL `pending()` re-offers a failure after this
MAX_BACKOFF = timedelta(hours=12)
# A video seen only through OF poster stills is not analysis of the clip unless
# the poster set is a real spread of it. Fewer than this stays unresolved.
POSTER_MIN_FRAMES = 3


def has_description(item) -> bool:
    """Nonblank operator-effective text; metadata flags alone are insufficient."""
    from vault_ai_effective import effective_value
    return any(isinstance(v, str) and bool(v.strip()) for v in
               (effective_value(item, 'description'), effective_value(item, 'video_description')))


def stamp_of(item) -> str | None:
    h = getattr(item, "content_hash", None)
    if not h:
        return None
    return f"{getattr(item, 'content_hash_kind', None) or ''}:{h}"


def fields_of(item) -> dict:
    out = load_json(getattr(item, "ai_fields_json", None), {}) or {}
    return out if isinstance(out, dict) else {}


def content_changed(item) -> bool:
    """True only when a recorded stamp exists and differs from the current one.
    Rows described before stamping existed are not automatically re-paid for.
    False means no observed mismatch, not proof that the full asset is unchanged."""
    recorded = fields_of(item).get("_content_stamp")
    current = stamp_of(item)
    return bool(recorded and current and recorded != current)


def content_version_evidence(item, recorded_stamp) -> dict:
    """Report the scope of an observed fingerprint, not visual correctness.

    The current vault hash producer fingerprints cached thumbnails. A matching
    hash cannot establish unchanged full video/photo bytes or accurate content
    interpretation. Unknown/legacy evidence stays explicit without scheduling
    automatic paid reanalysis of every pre-stamp asset.
    """
    current = stamp_of(item)
    status = 'unknown'
    if isinstance(recorded_stamp, str) and recorded_stamp and current:
        status = 'matching_fingerprint' if recorded_stamp == current else 'changed'
    elif isinstance(recorded_stamp, str) and recorded_stamp:
        status = 'unverifiable'
    kind = getattr(item, 'content_hash_kind', None) or ''
    return {'status': status, 'recorded_stamp': recorded_stamp, 'current_stamp': current,
            'fingerprint_scope': 'thumbnail' if kind.startswith('thumb/') else 'unspecified',
            'full_asset_verified': False}


def failure_of(item) -> dict | None:
    f = fields_of(item).get("_describe_failure")
    return f if isinstance(f, dict) else None


def sampling_note(kind: str, source: str, frames: int, duration: int | None) -> str:
    if kind != "video":
        return "single still image"
    span = f" from a {int(duration)}s clip" if duration else " from a clip"
    if source == "poster":
        return (f"{frames} platform preview stills{span}; timing unknown, audio and "
                "everything between the stills unseen")
    return f"{frames} frames sampled in order{span}; audio and unsampled moments unseen"


def next_failure(prev: dict | None, category: str, stamp: str | None, now: datetime):
    """(failure_dict, describe_generated_at). Attempts reset when content changed.

    Exponential backoff is carried by `describe_generated_at`: `pending()` offers a
    failed row again once that stamp is 30 minutes old, so a future-dated stamp
    delays the retry by exactly the extra backoff."""
    same = isinstance(prev, dict) and prev.get("stamp") == stamp
    attempts = (int(prev.get("attempts", 0)) if same else 0) + 1
    exhausted = attempts >= MAX_ATTEMPTS
    delay = min(BASE_RETRY * (2 ** (attempts - 1)), MAX_BACKOFF)
    return ({"category": category, "attempts": attempts, "exhausted": exhausted,
             "stamp": stamp, "at": now.isoformat()}, now + (delay - BASE_RETRY))


def merge_failure(item, failure: dict) -> str:
    fields = fields_of(item)
    fields["_describe_failure"] = failure
    return json.dumps(fields, ensure_ascii=False, default=str)
