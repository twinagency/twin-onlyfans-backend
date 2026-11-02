"""Internal media descriptions for Moxy-owned accounts.

Reuses the existing describe pipeline (`vault_ai_api._describe_one`, the same
call `automations/describe_media` makes) and stores nothing new: the description,
model, call id and frame count it writes on the vault row ARE the evidence.
This module only decides WHICH items need (re)describing, exposes an explicit
state per item, and refuses to run for an account Moxy does not own.

Integration point (engine, not edited here): call `await ensure_described(
account_id, media_ids)` on vault import/change and `await descriptions(account_id,
media_ids)` before building product facts; only state == "described" is usable.

States: described | pending (never attempted, or media changed since) |
failed (provider/fetch/refusal, retryable) | unsupported (kind cannot be described
here, or DRM-blocked with no poster frame) | missing (no such vault item).
"""
from __future__ import annotations

import os

from sqlalchemy import select

import vault_describe_state as vds
from db.engine import get_session
from db.models import VaultItem
from vault_ai_effective import effective_value, locked_fields

DESCRIBABLE_KINDS = ("photo", "gif", "video")
_UNSUPPORTED_STATUS = {"blocked_drm", "gone", "poster_only"}


def effective_description(item) -> str:
    """The description Moxy may use: operator-locked text first (for a video its own
    field, then the other), else the AI text. Never the raw column alone, so an
    operator correction always beats a model description."""
    if item is None:
        return ""
    order = ("video_description", "description") if item.kind == "video" else ("description", "video_description")
    locked = locked_fields(item)
    for field in [f for f in order if f in locked] + list(order):
        value = effective_value(item, field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def operator_corrected(item) -> bool:
    locked = locked_fields(item)
    return bool(locked & {"description", "video_description"}) and bool(effective_description(item))


def item_state(item) -> str:
    """described needs a description that is the operator's, or the model's for the
    CURRENT content. Unrelated metadata (expiring URLs, tags, prices) never makes a
    description stale; only a change of the recorded content stamp does."""
    if item is None:
        return "missing"
    if item.kind not in DESCRIBABLE_KINDS:
        return "unsupported"
    if operator_corrected(item):
        return "described"
    st = item.describe_status
    if st == "described" and effective_description(item):
        return "pending" if vds.content_changed(item) else "described"
    if st in _UNSUPPORTED_STATUS:
        return "unsupported"
    if st in (None, "capped"):
        return "pending"
    return "failed"        # failed, refused, fetch_failed, empty "described"


def limitations(item) -> dict:
    """What the description is based on, from the describe run's own record."""
    f = vds.fields_of(item)
    out = {k: f.get(k) for k in ("_frame_source", "_frames", "_sampling_note") if f.get(k) is not None}
    return {k.lstrip("_"): v for k, v in out.items()}


def _view(media_id, item) -> dict:
    state = item_state(item)
    out = {"media_id": int(media_id), "state": state}
    if item is not None:
        out["evidence"] = {
            "describe_status": item.describe_status, "model": item.describe_model,
            "call_id": item.describe_call_id, "frames": item.frames_sampled,
            "generated_at": item.describe_generated_at.isoformat() if item.describe_generated_at else None,
            "content_hash": item.content_hash,
            "operator_corrected": operator_corrected(item),
            "failure": vds.failure_of(item),
        }
        if state == "described":
            out["description"] = effective_description(item)
            out["limitations"] = limitations(item)
    return out


async def descriptions(account_id: str, media_ids) -> list[dict]:
    ids = [int(m) for m in media_ids]
    async with get_session() as s:
        rows = (await s.execute(select(VaultItem).where(
            VaultItem.account_id == account_id, VaultItem.media_id.in_(ids)))).scalars().all()
    by = {r.media_id: r for r in rows}
    return [_view(m, by.get(m)) for m in ids]


MAX_ENSURE = 20     # per call; larger imports are described by later calls/sweeps


async def ensure_described(account_id: str, media_ids, *, describe=None, retry_failed=False,
                           max_items: int = MAX_ENSURE) -> list[dict]:
    """Describe pending items (and failed ones when `retry_failed`) for a
    Moxy-owned account, then return every item's state. A failure is recorded
    by the describe pipeline and reported as `failed`; it never raises."""
    import asyncio
    from moxy_replacement import routing
    if await asyncio.to_thread(routing.owner_state, account_id) != routing.SELECTED:
        raise routing.Refused("moxy_account_required")
    if describe is None:
        if account_id in scoped_accounts():
            # The account-scoped worker owns these descriptions (own credential,
            # bounded batches, one child per account). The global-credential path
            # here would fail without a key and mark items failed: report only.
            return await descriptions(account_id, media_ids)
        from vault_ai_api import _describe_one as describe
    before = await descriptions(account_id, media_ids)
    todo = [r for r in before if r["state"] == "pending" or (retry_failed and r["state"] == "failed")]
    for row in todo[:max(0, int(max_items))]:
        try:
            await describe(account_id, row["media_id"])
        except Exception:  # noqa: BLE001 - recorded state is re-read below
            pass
    return await descriptions(account_id, media_ids)


def scoped_accounts() -> set[str]:
    return {a.strip() for a in os.environ.get("ONLYFANS_VAULT_DESCRIPTION_SCOPED_KEYS", "").split(",") if a.strip()}
