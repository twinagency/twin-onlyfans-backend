"""Ownership and history derived from the existing immutable control ledger."""
from sqlalchemy import select
from db.onlyfans_models import OfControlAudit


def takeover_owner(session, account, fan, state):
    if state is None or state.takeover_state == "none":
        return None
    return session.scalar(select(OfControlAudit.actor).where(
        OfControlAudit.account_id == account, OfControlAudit.fan_id == fan,
        OfControlAudit.revision <= state.revision,
        OfControlAudit.action == "request_takeover",
        OfControlAudit.takeover_from == "none",
        OfControlAudit.takeover_to.in_(("requested", "effective")),
    ).order_by(OfControlAudit.revision.desc()).limit(1))


def staff_owns_takeover(session, account, fan, state, actor):
    # Internal operator sessions retain recovery authority. Agency staff must
    # own the current takeover, including when a queued send reaches dispatch.
    return not actor.startswith("agency-user:") or takeover_owner(session, account, fan, state) == actor


async def read_handover(session, account, fan, tenant_id=None):
    from db.models import Fan, OfConversationState, OfCreatorBinding
    from onlyfans_read.agency_scope import lock_read_scope
    from onlyfans_read.errors import Refused
    from onlyfans_read.contract import iso, conversation_id_for
    binding = select(OfCreatorBinding.id).where(OfCreatorBinding.account_id == account,
        OfCreatorBinding.channel == "onlyfans", OfCreatorBinding.revoked_at.is_(None))
    if tenant_id is not None: binding = binding.where(OfCreatorBinding.tenant_id == tenant_id)
    if await session.scalar(binding.with_for_update(read=True)) is None:
        raise Refused("unknown_account")
    await lock_read_scope(session, account)
    if await session.get(Fan, (account, fan)) is None:
        raise Refused("unknown_conversation")
    state = await session.get(OfConversationState, (account, fan))
    revision = state.revision if state else 0
    takeover = state.takeover_state if state else "none"
    owner = await session.run_sync(lambda s: takeover_owner(s, account, fan, state))
    rows = (await session.scalars(select(OfControlAudit).where(
        OfControlAudit.account_id == account, OfControlAudit.fan_id == fan,
        OfControlAudit.revision <= revision).order_by(OfControlAudit.revision.desc()).limit(31))).all()
    return {"schemaVersion": "of-handover.v1", "accountId": account,
        "conversationId": conversation_id_for(account, fan), "revision": revision,
        "mode": state.mode if state else "paused", "takeoverState": takeover, "ownerActor": owner,
        "hasMore": len(rows) > 30, "events": [{"action": row.action or "change_mode", "mode": row.mode,
            "revision": row.revision, "actor": row.actor, "at": iso(row.created_at)} for row in rows[:30]]}
