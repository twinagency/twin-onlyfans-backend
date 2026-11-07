# Plan C decision register (model integration lane)

## Model and fallback
- Primary model: `deepseek/deepseek-v4.1-flash` via OpenRouter. Owner decision (Ronan), 2026-11-07.
- Fallback model: `x-ai/grok-4.1-fast` (cheap, NSFW-capable). **Ronan decision 2026-11-07; Jesse decision not yet recorded; release config default OFF.**
  - Enable only with `ENGINE_MODEL_FALLBACK_ENABLED=1`, `ENGINE_MODEL_FALLBACK=<id>` and its own verified prices
    (`ENGINE_MODEL_FALLBACK_PROMPT_MICROUSD_PER_MTOK`, `ENGINE_MODEL_FALLBACK_COMPLETION_MICROUSD_PER_MTOK`). Anything missing is a startup refusal.
  - At most ONE fallback. No lists, no auto routing, no `ENGINE_MODEL_ALTERNATE` (refused).
  - Used ONLY when the primary call failed for availability reasons: connect error (not sent), HTTP 429/408/5xx, timeout.
    NEVER after a refusal, an empty completion (how DeepSeek via OpenRouter refuses), malformed output or a draft the validator rejected:
    those become visible human work (handover plus operator case `model_handover`). 400/401/403/404 and unclassified client errors never fall back.
  - Each model has its own required per-token prices and its own conservative reservation. Both sit inside the SAME daily and per-creator caps and the SAME
    total-attempt bound (default 2 invocations per request generation). A timed-out primary stays an `unknown` reservation (full upper bound) while the fallback runs.
  - Every call row records the requested model (`model`), `chain_step` (`primary` / `retry` / `fallback`); a differing or unreported returned model is flagged in
    `error_class` (`ret_model:<id>`) and the full identity (requested, returned model, returned provider, prompt version, config version, chain step, is_fallback)
    is written to the `conversation_engine.model` log. The ledger has no column for the returned id (no migration allowed in this lane): a column is the
    proper home and is listed as an open item.
- Prices are required configuration. No network call is ever made to look them up; an enabled runtime with unset prices refuses to start.
- The writing prompt is model-agnostic (`ModelRuntime.render`): the same messages go to either model.

## Writing and sales (pilot rules)
- One versioned prompt, `a-original.v2-planc1`, through the existing prompt-provider seam. NSFW-capable inside creator boundaries (no minors, no meet-ups, nothing off-platform).
- Fixed price, no automatic discount, no ladder, no bundles, no timed outreach. A refusal or affordability concern stops the pitch and invalidates unsent sales drafts.
- Temporary "not now" is not a permanent ban; the fan's clear renewed request reopens. The optional 12-hour eligibility on a LATER inbound message is implemented but
  OFF (`JourneyConfig.temporary_pause_reopen_hours=None`) until Jesse records it. An explicit "stop offering" is never reopened by time.
- Deferred and removed from `journey.py`: welcome offer, whale hold/flag thresholds, ladder defaults.
