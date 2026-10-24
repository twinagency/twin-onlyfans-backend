"""Model seam for the content resolver and its prompts (same pattern as ai_chatter._runtime_chat).

With no Telegram capability snapshot active both helpers are the legacy calls, unchanged.
With one active (the native pilot runner), the model is resolved and called through the
capability, so a process whose legacy ``llm_client.chat`` is unavailable still reaches the
pinned, counted, budgeted model. ``llm_client`` is looked up at call time so tests can patch it.
"""
from __future__ import annotations

import llm_client
from telegram_runtime.capabilities import active_telegram_capabilities


async def runtime_chat(**kwargs):
    cap = active_telegram_capabilities()
    if cap is not None:
        return await cap.chat(**kwargs)
    return await llm_client.chat(**kwargs)


async def runtime_resolve_model(legacy, account_id, purpose, override=None):
    cap = active_telegram_capabilities()
    if cap is not None:
        return await cap.resolve_model(account_id, purpose, override)
    return await legacy(account_id, purpose, override)
