# Plan C operator contract (backend: join branch, dashboard: planc-dash)
Backend module conversation_engine/approval.py (being written by the approval agent) exposes approve/reject/edit/list. HTTP routes (new file onlyfans_commands/engine_approval_api.py, same auth as engine_operator_api: bearer token, schema header X-OnlyFans-Engine-Operator-Version: of-engine-operator.v2, JSON, <=8KB, idempotent commandKey) under /onlyfans/control/v2/accounts/{account}/ :
- GET  drafts                        -> pending/awaiting drafts with exact ordered parts, action (reply|offer), product/media ids+count, price/currency, draftVersion, draftHash, expiresAt, conversation context (last 10 messages), status, unresolved evidence summary
- POST drafts/{requestId}/{generation}/approve   {expectedVersion, commandKey, actor}
- POST drafts/{requestId}/{generation}/reject    {expectedVersion, commandKey, actor, reason}
- POST drafts/{requestId}/{generation}/edit      {expectedVersion, commandKey, actor, parts:[text...]}  (re-validated; invalidates prior approval)
- POST conversations/{conversationId}/stop | takeover | handback  {commandKey, actor, expectedRevision}  (handback restores planning eligibility only, never replays drafts)
- GET  unresolved                    -> unknown sends, unlinked transactions, unreconciled access evidence, each with owner + next step
- POST unresolved/{id}/reconcile     {commandKey, actor, evidenceRef}
Dashboard: one page /onlyfans/plan-c (operator only) that lists drafts with the exact payload, approve/reject/edit buttons, stop/takeover/handback, unresolved list. Minimal UI, server-side proxy route that holds the token (never expose the token to the browser).
