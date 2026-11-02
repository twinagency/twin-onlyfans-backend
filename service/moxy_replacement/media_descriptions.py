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

from sqlalchemy import select

from db.engine import get_session
from db.models import VaultItem

DESCRIBABLE_KINDS = ("photo", "gif", "video")
_UNSUPPORTED_STATUS = {"blocked_drm", "gone"}


def item_state(item) -> str:
    if item is None:
        return "missing"
    if item.kind not in DESCRIBABLE_KINDS:
        return "unsupported"
    st = item.describe_status
    if st == "described" and (item.description or item.video_description):
        stale = (item.updated_at and item.describe_generated_at
                 and item.updated_at > item.describe_generated_at)
        return "pending" if stale else "described"
    if st in _UNSUPPORTED_STATUS:
        return "unsupported"
    if st in (None, "capped"):
        return "pending"
    return "failed"        # failed, refused, fetch_failed, empty "described"


def _view(media_id, item) -> dict:
    state = item_state(item)
    out = {"media_id": int(media_id), "state": state}
    if item is not None:
        out["evidence"] = {
            "describe_status": item.describe_status, "model": item.describe_model,
            "call_id": item.describe_call_id, "frames": item.frames_sampled,
            "generated_at": item.describe_generated_at.isoformat() if item.describe_generated_at else None,
            "content_hash": item.content_hash,
        }
        if state == "described":
            out["description"] = item.video_description or item.description
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
        from vault_ai_api import _describe_one as describe
    before = await descriptions(account_id, media_ids)
    todo = [r for r in before if r["state"] == "pending" or (retry_failed and r["state"] == "failed")]
    for row in todo[:max(0, int(max_items))]:
        if True:
            try:
                await describe(account_id, row["media_id"])
            except Exception:  # noqa: BLE001 - recorded state is re-read below
                pass
    return await descriptions(account_id, media_ids)
