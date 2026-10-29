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
    try:
        memory = snapshot(session, account_id, fan_id, query=(last_inbound or "")[:2000])
        memory = {k: v for k, v in memory.items() if k not in {"version", "revision"}}
        extra = bool(memory.get("learnedProfile") or memory["interactions"] or memory["overrides"]
                     or any(f["source"] != "canonical" for f in memory["facts"].values()))
    except Exception:
        session.rollback()
        memory, extra = {}, False
    return PromptContext(persona=persona, dated_memory="learnedProfile" in memory,
                         finish=(lambda messages: augment_prompt(messages, memory)) if extra else None)


class TwinPromptContext:
    async def prompt_context(self, account_id: str, fan_id: int, *, last_inbound: str) -> PromptContext:
        from db.engine import get_session
        async with get_session() as s:
            return await s.run_sync(lambda sync: _build(sync, str(account_id), int(fan_id), last_inbound))
