# CRM video repairs, 28 October 2026

This is the implementation and release checklist. Local results are separate from production acceptance. The 28 October activation record supersedes the earlier hold; schema 0098 is retained. No account or fan automation settings are broadly enabled.

## Verified baseline

- Production schema: `0098_ledger_exclusion`.
- API and FASTT: source `7f4eaf0bcb0b461300a0301330acd773f2b3d8f0`, image `bc2a3dc7e012d66a3c442b7fe4eff0a76d1bfca4c7660b0f050eb7dfd562830e`.
- Worker: image `c3ef3b6f3932df063f0fd94b5ea2996fc4b3ce02f77318ac4fd885a787b6a21a`. All 718 Python files compared against the API image; only `onlyfans_runtime/recent_inbox.py` differs. Its PR #38 continuation fix is preserved.
- Dashboard advanced during this work to `38d6ac20d33403339a635c9668e5ef0189573ea1`, image `fbe7ea1c86329d1fece091eee14300fb166b9a693001ef3f3bc30e6ce81c06fb`. Its sync-warning and connection-link changes are incorporated.
- Nicky and Naomi enabled, sender heartbeats running. Maile, Katie Price and Alva Jay remain runtime-disabled.
- Manual failure reproduced through the browser. Expected/current revision `7/7`, effective human takeover, no unresolved attempt, account and policy enabled, allowance `200/500`, queue empty. Authority reason `history_quarantined`; no test intent was created.
- Verified quarantine: Nicky 141, including 140 on the designated test fan. Maile 1,007. All text differences are equivalent provider HTML. Four Nicky records also have missing legacy message media arrays; their exact ordered media IDs exist in account/fan/message-scoped `vault_sends`.

## Checklist

| Issue | Reproduced | Implementation | Local verification | Deployed/live |
| --- | --- | --- | --- | --- |
| Manual reply after takeover | Browser and authority | Representation-only intake matching and audited idempotent recovery; raw observations, message IDs, budgets and all send guards retained | Pure regressions passed; disposable PostgreSQL acceptance in progress | Pending |
| Specific refusal reasons | Source and browser | Allowlisted codes end to end, revision/hold diagnostics, composer feedback | Control transport and refusal tests passed | Pending |
| Effective AI status | Runtime flags and supervisor heartbeat verified | Saved fan setting separated from account, connection, sender freshness and holds | AI visibility and control tests passed | Pending |
| Delivery states and resolution | Ledger/projection inspected | In-flight, accepted pending projection, delivered, failed and unknown are distinct; exact receipt scope; positive-only audited resolution with server-side provider verification | Existing ledger/recovery checks passed; HTTP/PostgreSQL verification pending | Pending |
| List/thread/control consistency | Recording and source | Refresh thread on new provider identity/activity; preserve loaded history, drafts and uncertain command identity; reject late sequences | Inbox refresh and selection checks passed | Pending |
| Server search | Recording and source | Bound/account-scoped name, username and numeric search; copied handle normalization; query-bound cursors and pagination | Normalization and transport checks passed | Pending |
| Sync/loading | Browser and source | Separate connection, intake, history, financial and sender observations; attention counts; preserve background content | Sync warning and empty/loading checks passed | Pending |
| Presentation/data | Browser and source | Provider profile display fields, safe fan avatars, reconciled spend with coverage, separate attention reasons, recorded authorship, Brisbane display time, action-specific pending feedback | UI and time checks passed | Pending |

Local evidence: 101 preview checks, 22 focused Python tests plus 10 subtests, read-service script, control transport, AI visibility, sync warning, UI, inbox refresh and TypeScript checks. These are synthetic/local checks, not native OnlyFans delivery proof.

## Recovery and release

1. Recheck current container IDs and source hashes before any swap. Abort on unreviewed drift. Preserve the dirty laptop/server checkouts.
2. Use only the disposable PostgreSQL test container and internal test network for regression data. No production credentials or provider/model network access enter those tests.
3. Build code overlays over the verified running dependency images. Preserve the newer worker continuation change. No schema migration is required.
4. For history recovery, call `history_repair.inspect` for one authorized account/fan, then `repair` with its exact fingerprint, a stable command key, an authenticated/internal import actor and a reason. Repeat the same key safely. Changed evidence refuses. Only equivalent text and exact retained pack identities qualify; substantive differences remain quarantined.
5. Recheck account and policy state. Do not clear intentional pauses, ownership, unknown delivery holds or allowance counters.
6. Live text journey is limited to the designated Nicky test conversation. Verify native OnlyFans/provider IDs and exactly one durable intent/receipt. Other creators stay unchanged. Unapproved purchases and arbitrary fan messages are excluded.
7. Delivery resolution accepts positive evidence only. Missing history never proves failure. Enable its capability only after the scoped endpoint and original recovery authorities pass their tests.

## Rollback

Keep stopped image-retention containers and private compose snapshots for the exact pre-swap images. Restore only each changed service image, using the same compose project/configuration and `up -d --no-deps --no-build --pull never`. Keep schema 0098. Do not restore an older database dump over newer receipts, reset send allowances, erase unknown attempts or delete raw intake. Verified history repairs retain immutable audit and source evidence; later corrections require a new audited decision.

Live acceptance, final image/commit identities and remaining blockers must be appended after release. Pending rows must not be described as passed or deployed.
