# Requested capability and API documentation

Support creators with 100,000 stored fans and approximately 10,000 distinct chatting fans per day. Process independent fans concurrently. Target replies between 25 seconds and three minutes using an agreed grouped-message timer and hard waiting deadline. Support text, images, video, paid media, exact manual caption/price and confirmed delivery.

Audit account and fan limits, workers, queue age, provider/API budgets, model latency, database/pool capacity, CPU/memory, media delivery, retry/reconciliation, duplicate prevention, manual/AI conflicts, takeover, tenant isolation, monitoring, backup and recovery.

For each issue provide evidence, impact, severity, attribution (FASTT, Twin, provider, infrastructure, data/sync, UI, configuration or unknown), fix, owner, tests, deployment and rollback.

## Acceptance work

- Full-size datasets for 1, 10 and 100 creators, with responsive paginated inbox/search and resumable bulk controls.
- Proposed planning workload: 10 reply turns per active fan, 100,000 turns/day/creator and peaks five times the daily average. Confirm the commercial envelope before committing.
- Burst: 1,000 fans message a creator in one minute while imports run. Account for every input, deadline and receipt.
- No lost inputs or duplicate sends; no wrong attachment, caption or price; uncertain sends reconciled before retry.
- Human takeover cancels pending AI work and checks ownership immediately before transport.
- Complete explicit-request and proactive-sale cycles, including changed request, ignored offer, owned media, insufficient catalogue, purchase/unlock and follow-up.
- Inject model timeout, malformed output, 429, session/proxy failure, lost receipt, repeated/out-of-order input and crash during send.
- Run a 24-hour mixed-load soak, deployment/restart and restore checks, followed by a controlled provider canary. Use synthetic traffic for stress tests.
- Report p50/p95/p99/max, deadline misses, unanswered turns, duplicates, queue age, provider failures, media failures, database health and recovery time. The requested three-minute maximum remains the acceptance target; a 99% draft is not approval to ignore the rest.

## Central API reference requested

Document the actual implementation, including base URL, version, authentication, scoped credentials, required headers, permissions, rate limits, timeouts, errors, idempotency and example requests/responses.

Cover creator/account, fan/conversation, inbound messages, text/media/paid sending, uploads, delivery status/confirmation, purchases/reconciliation, human takeover, AI controls, safe retry and webhooks where implemented. Mark unsupported capabilities explicitly. Include sandbox/test access and production access process. Keep secrets out of committed documentation. No verified base URL, API credential or endpoint contract was produced by this audit.