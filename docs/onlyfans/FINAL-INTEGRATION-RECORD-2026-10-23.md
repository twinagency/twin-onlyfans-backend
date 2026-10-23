# Final integration execution record

This record is in progress. No completion or deployment is claimed.

## Candidate inputs

- Backend combined input: `aeac726061c046aaa10916b5d7df5fa731e89656` (PR19).
- Dashboard combined input: `c64fe11abf3be5fde9b6bedaadd7345ad6ecfa40` (PR252).
- Recent branches were inventoried across 30 backend refs (28 distinct heads) and 428 dashboard refs (414 distinct heads). No branch was deleted.
- Concurrent combined work was reconciled through backend `e7eae43485a053bb74b9efef3f13b577cee41f48` (18 additional commits) and dashboard `710005d67c99db46e62c6b133a7e259821aa8846` (six additional commits). The selected implementation also retains the newer OnlyFans UI input `28fd0038966550c733c11fbfe5c53d52403eed1f` and current production-main contributions. Exact final ancestry is recorded with the immutable release pair.
- Backend deployment branch has three merge ancestors absent from combined. A real git merge-tree rehearsal against deployment1614696 returned the unchanged combined tree61ca0699428ff8e27aab7b0e4543eaf1fac97765, proving those merge ancestors can be preserved without dropping content.
- Current implementation starts from byte-verified combined snapshots. Acquisition excludes unrelated legacy frontend assets; no deployable full-source artifact has yet been built.
- Paired dashboard: `1805ebd7e046537f047d31212fdd5fda4454fed1`, tree `1a01d21b633000e833e05e7873c02107da0c944d`, published on `codex/onlyfans-final-release-20261023` and pinned in `ACCEPTANCE-DASHBOARD-SHA`. Its parents preserve the prior integrated candidate, UI input and latest production main `979e097946dad0c1aa3cb83680dc584cb28e6533`. The earlier `363d9d40694c6847f5bf7644db3e09e3729ee005` candidate is superseded. Full acceptance remains pending.
- PostgreSQL16.15, Python3.12.14 and Chromium153.0.8010.0 are available. The actual hybrid dashboard/backend rehearsal passed all four cases with zero failures, errors or skips, including backup/restore and retained unknown outcomes. Its tab-hydration race was reproduced and corrected before the passing run. A clean production dashboard build and subsequent TypeScript check also passed. These targeted results do not substitute for exact-pair full acceptance.

## Checklist

| Plan section | State | Evidence / next action |
| --- | --- | --- |
| 1 Candidate / release path | Baseline verified | Exact pins and real merge-tree preservation proof; final source/artifact pair pending |
| 2 Four known failures / environment | Repairs implemented / targeted verified | Isolation and populated historical migrations pass; hybrid real rehearsal passes all four cases |
| 3 Backend / dashboard contracts | Connected / targeted verified | Purpose-separated transport, strict native adapters and actual hybrid rehearsal pass |
| 4 History / memory | Connected / targeted verified | Scoped stable fan pages, field/version corrections, separate coverage measures and source retrieval |
| 5 Personality / AI | Connected / targeted verified | Document approval/history, effective prior approvals, authoritative credential/model/budget readiness |
| 6 Engagement / account controls | Connected / targeted verified | Durable stop/resume, no old queue release, event invalidation and stable action pages |
| 7 Ledger | Connected / targeted verified | Purpose-separated stored projections, durable retry and exact evidence resolution |
| 8 Eight content-sync findings | Implemented / targeted verified | All 15 concurrent findings scenarios adapted; 23 tests pass without skips. Durable receipts, occurrences, independent priority rotation, minimum archive progress, cursor audit, read fencing and fresh upload authority retained |
| 9 Account readiness | Connected / targeted verified | Current-binding evidence, effective approval, actual worker observations and specific corrective controls |
| 10 Combined acceptance | Pending | Targeted tests first, then one recorded candidate pair and zero required skips |
| 11 Release / live verification | Pending | Independent implementation and verification precede live authorization review |

## Completion labels

- Code complete: **No**.
- Integrated and accepted: **No**.
- Deployed: **No release performed**.
- Live verified: **No**.
- Autonomous handling enabled: **Not verified; no activation performed**.

Tests, final SHAs, immutable artifacts, actual versions and real-account observations will be recorded only when obtained.

## Reconciliation decisions

The canonical flat ledger read remains GET `/onlyfans/api/v1/accounts/{id}/ledger`
with the concurrent `of-ledger.v1` contract. Its control-purpose retry uses
the current connection revision and complete scan fingerprint; the immutable
`LedgerRetryCommand` is the single retry audit owner. Rich paginated evidence is
read at `/ledger/evidence`; evidence-backed resolution remains POST
`/onlyfans/control/v1/accounts/{id}/ledger/resolve`. Account stop/resolution use
the separate immutable launch-command audit. Old-binding scan details and
evidence are hidden; retained ambiguous evidence can still hold the account.

The concurrent retry table is introduced by additive `0091_launch_controls`,
with its append-only guard. The published input `0090_launch` snapshot is not
rewritten. Selective engagement event cancellation and the metadata-only
credential callback are retained, with current-binding fencing and no provider
client construction. Personality commands from the dashboard also compare the
displayed binding inside the locked native transaction, including on replay.

## Operational monitor verification

The existing generic VPS watchdog checks containers, host resources and an HTTP
route. It does not establish that OnlyFans queues or retained outcomes are
healthy. `onlyfans_runtime.launch_monitoring` now evaluates a fresh successful
private `of-operations.v3` read. It emits fixed-text actionable conditions for
stalled waiting work, an unavailable connection, retained unknown transport,
quarantined intake and supported operational holds. Invalid, stale, future or
missing input fails closed; provider errors and message bodies are not echoed.
An intentionally stopped account is not itself an alert.

An authorized monitor can pass its scoped JSON read on stdin:

```sh
PYTHONPATH=service python -m onlyfans_runtime.launch_monitoring < private-operations.json
```

Exit codes are 0 (clear), 1 (attention), and 2 (input unavailable). The adapter
does not fetch credentials, mutate account state or send notifications. Its
synthetic behavior is tested; the actual private HTTP projection is checked in
the combined HTTP test. Production scheduling and notification delivery remain
unverified and must be recorded during the authorized release.

The worker also provides a separate one-shot paid-post cursor verification
mode: `--verify-post-cursor-account`, `--command-key`, and `--actor`. It requires
the existing explicit worker tenant/connection/database authority, never enters
the normal worker cycle, and performs only the owner's two bounded provider
read pages plus durable session-bound audit. Actual provider validation is a
later scoped live check; tests do not make that contract live-verified.
