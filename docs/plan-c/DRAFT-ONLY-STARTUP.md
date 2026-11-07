# Plan C inactive draft-only application path

This is an offline-reviewed integration candidate, not a release or permission to make model/provider calls. The application starts no Plan C worker by default. No configuration or service has been activated by this change.

The opt-in `TWIN_PLAN_C_DRAFT_ONLY=1` builds the existing `RealConversationWorker` with `DraftOnlyTransport`, which has no provider client or send method. Startup and each event require the shared account delivery policy to be a disabled `fansapi` policy. A later policy enable causes the worker to refuse work; the existing approval hash also binds the policy revision. This mode has no approved-dispatch endpoint and cannot deliver even an approved draft. Do not run an old sender against the new schema.

Before any separately authorised model qualification, configure and review all values in a private environment:

- `TWIN_PLAN_C_TENANT` and `TWIN_PLAN_C_ACCOUNT`: one existing binding.
- `TWIN_PLAN_C_FANS`: an explicit, comma-separated set of numeric fan IDs, at most 100.
- `TWIN_PLAN_C_ITEM_ID`, `TWIN_PLAN_C_PRICE_CENTS`, `TWIN_PLAN_C_MEDIA_IDS`, and `TWIN_PLAN_C_PREVIEW_IDS`: the single approved catalogue item, exact price in cents, and ordered comma-separated media and preview IDs (the preview value can be empty). Startup and every event compare these to the current enabled account-scoped catalogue item. The creator profile must permit exactly this item.
- `TWIN_PLAN_C_WEBHOOK_SECRET_FILE`: a private, user-owned 0600 regular file outside a Git worktree; never commit or log its contents.
- The existing `ENGINE_MODEL_*` settings including a single explicit primary, one-model allowlist, one-account allowlist, approved creator profile file, token-price bounds and durable model budget caps. Model fallback and the temporary-pause reopening feature must remain off.

The only added route is `POST /onlyfans/webhooks/plan-c-draft`. It verifies `Signature` against the exact raw bytes and requires a single `X-OFAPI-Idempotency-Key`; a disabled mode returns 404. Bodies are bounded to 1 MB. The existing intake commits raw evidence before projection and draft generation. A successful HTTP acknowledgement means the worker completed processing, not that a draft was produced or a model call succeeded. An authenticated redelivery uses the same durable event/request records. If processing throws after intake, HTTP 503 allows redelivery without fabricating another event. This synchronous route waits for model processing and has not been qualified against the live provider's retry and timeout behavior.

The account/fan/product checks here apply to this no-send process only. They are not a durable cross-process pilot configuration, do not bind the operator session to a named human, do not inventory provider-side queues or establish a restart-safe Fans API credit cap, and do not start an approved live dispatcher. Those remain separate release blockers.
