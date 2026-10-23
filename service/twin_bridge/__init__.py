"""Fastt -> Twin reply bridge (Stage 1C part 1).

Two offline-tested pieces:

* `intake`   - durable, account-scoped intake of eligible inbound text DMs that
                Fastt has already persisted (the hook and the reconciliation).
* `client`   - an import-safe async HTTP client for Twin's draft + status routes.

Nothing here sends a message, talks to Twin from the ingestion path, or runs a
worker: grouping, generation orchestration and delivery coordination are later
stages. Intake is disabled unless trusted scope is configured.
"""
