"""Twin's approved creator context for the FASTT prompt, read from Twin's own data.

Supplies the ONE prompt hook of ``ai_chatter`` (``_runtime_prompt_context``) with
exactly what Twin's native planner already uses and nothing new:

* the approved persona prose plus the effective creator facts
  (``creator_facts.effective``: Twin source overrides first, then the saved
  facts), composed by FASTT's own ``compose_persona``;
* the scoped historical fan memory (``launch_history_memory.snapshot``), added
  once as evidence after FASTT composes its prompt, only when it holds more than
  the canonical fan columns FASTT already renders.

It writes nothing, creates no store, and refuses (fixed public reasons only)
when the persona is not approved or the shared creator facts are not current,
as Twin's native path does. Truthful identity wording is not here: it is the
``twin_context`` flag of ``ai_chatter._build_messages``.
"""
from __future__ import annotations

from types import SimpleNamespace

from telegram_runtime.capabilities import PromptContext


class TwinContextRefused(RuntimeError):
    """Fixed public reason only."""


def _build(session, account_id: str, fan_id: int, last_inbound: str) -> PromptContext:
    from db.models import AccountAiConfig
    from automations._persona import compose_persona
    from onlyfans_runtime.creator_facts import effective
    from onlyfans_runtime.launch_history_memory.memory import augment_prompt, snapshot
    from onlyfans_runtime.native_context import approved_persona, canonical

    cfg = session.get(AccountAiConfig, account_id)
    if cfg is None or not approved_persona(cfg.persona):
        raise TwinContextRefused("creator-persona-not-approved")
    facts, source = effective(session, cfg)
    if source is not None and source.get("state") != "current":
        raise TwinContextRefused("creator-facts-not-current")
    # A transient view: never mutate the ORM row (autoflush would persist it).
    view = SimpleNamespace(persona=cfg.persona, location=getattr(cfg, "location", None),
                           voice=getattr(cfg, "voice", None),
                           persona_facts_json=canonical(facts) if facts else None)
    persona = compose_persona(view, fallback="", fact_value_clip=16000)
    # The persona and the creator facts above are genuine holds (identity and claims). Fan MEMORY is evidence
    # added when it exists: an incomplete history import, a refresh in progress or an unreadable memory row
    # must never stop an ordinary text reply, so a failure here answers without it.
    memory, overrides = {}, {}
    try:
        memory = snapshot(session, account_id, fan_id, query=(last_inbound or "")[:2000])
        memory = {k: v for k, v in memory.items() if k not in {"version", "revision"}}
        overrides = memory.get("overrides") or {}
    except Exception:
        session.rollback()
        memory = {}
    learned = memory.get("learnedProfile")
    # "dated memory" means learned memories actually exist for THIS fan. (The learner returns an envelope even for a fan
    # with none; treating that as memory used to switch the canonical columns and the profile bio off for every fan.)
    has_learned = bool(isinstance(learned, dict) and learned.get("memories"))
    extra = bool(has_learned or memory.get("interactions") or overrides
                 or any(f["source"] != "canonical" for f in (memory.get("facts") or {}).values()))
    if memory and not has_learned and isinstance(learned, dict):
        memory = {k: v for k, v in memory.items() if k != "learnedProfile"}
    return PromptContext(persona=persona, dated_memory=has_learned,
                         finish=(lambda messages: augment_prompt(messages, memory)) if extra and memory else None,
                         name_override=_given_name(session, account_id, fan_id),
                         suppress_fields=frozenset(overrides))


def _given_name(session, account_id: str, fan_id: int):
    """The name this fan gave in his own recent messages (latest wins); scoped to this account and fan only."""
    from sqlalchemy import select
    from db.models import Message
    from .fan_name import given_name
    try:
        rows = session.execute(select(Message.body).where(
            Message.account_id == account_id, Message.fan_id == fan_id, Message.direction == "in",
            Message.is_unsent.is_(False)).order_by(Message.created_at.desc(), Message.message_id.desc()).limit(80)).all()
    except Exception:
        session.rollback()
        return None
    return given_name([r[0] for r in reversed(rows)])


class TwinPromptContext:
    async def prompt_context(self, account_id: str, fan_id: int, *, last_inbound: str) -> PromptContext:
        from db.engine import get_session
        async with get_session() as s:
            return await s.run_sync(lambda sync: _build(sync, str(account_id), int(fan_id), last_inbound))
