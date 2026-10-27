# Final integration execution record

This record is in progress. No completion or deployment is claimed.

## Candidate inputs

- Backend combined input: `aeac726061c046aaa10916b5d7df5fa731e89656` (PR19).
- Dashboard combined input: `c64fe11abf3be5fde9b6bedaadd7345ad6ecfa40` (PR252).
- Recent branches were inventoried across 30 backend refs (28 distinct heads) and 428 dashboard refs (414 distinct heads). No branch was deleted.
- Concurrent combined work was reconciled through backend `e7eae43485a053bb74b9efef3f13b577cee41f48` (18 additional commits) and dashboard `710005d67c99db46e62c6b133a7e259821aa8846` (six additional commits). The selected implementation also retains the newer OnlyFans UI input `28fd0038966550c733c11fbfe5c53d52403eed1f` and current production-main contributions. Exact final ancestry is recorded with the immutable release pair.
- Backend deployment branch has three merge ancestors absent from combined. A real git merge-tree rehearsal against deployment1614696 returned the unchanged combined tree61ca0699428ff8e27aab7b0e4543eaf1fac97765, proving those merge ancestors can be preserved without dropping content.
- Current implementation starts from byte-verified combined snapshots. Acquisition excludes unrelated legacy frontend assets; no deployable full-source artifact has yet been built.
- Paired dashboard: `c75e101a69a07aa5a095261d1516c552741403e1`, tree `a8f32972458acbc953f23d974e2f3e4f2c1b85d9`, prepared for `codex/onlyfans-final-release-20261023` and pinned in `ACCEPTANCE-DASHBOARD-SHA`. Its two parents preserve the fully reconciled candidate `4a860a5eb13f5130ff42768e6d5ed44dea065837` and subsequently reviewed main `656216336f1f1c2c3415a6e85cf03ec9f698add0`. Earlier combined, UI and production-main contributions remain ancestors. Full native acceptance on backend `65d3ef5d3541ddff8d67397a992ac0c4c2b845b3` and dashboard `4a860a5eb13f5130ff42768e6d5ed44dea065837` is recorded separately from validation of this four-path successor. All backend product, test, manifest, migration, dependency and build inputs are unchanged; only this record and the dashboard pin change. Full release acceptance remains pending.
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

## Final coverage correction

The first exact-pair candidate was backend
`abd3c03e4d9c7aa263c60a02268d78906c2cf8b8` with dashboard
`1805ebd7e046537f047d31212fdd5fda4454fed1`.
Its full manifest collected 1,918 nodes without collection errors or
deselections. Root interrupted execution after observing 72 passing nodes
because a separate source review confirmed a reporting defect. Exit 130 and
the missing final JUnit/summary are incomplete acceptance, never a pass.

A terminal partial or failed source-detail refresh retains its outcome but
previously disappeared from the aggregate pending count. A fresh local scan
could therefore label imported paid messages current while provider details
remained unresolved. Coverage now remains partial with fixed operator text
until authoritative successful observation clears that current source receipt's
error. Prior terminal job outcomes and other bindings remain intact. Scheduling
completion still means all work was persisted, rather than provider recovery
completed. Real database regressions exercise both terminal paths and recovery.
Both regressions failed at the intended false-current assertion before the
product fix, then passed afterward. The focused 30-case queue, coverage, auth,
API and findings run passed with zero failures, errors or skips. These runs
overlap and are not a unique release total.

The corrected backend requires its own exact-SHA full acceptance. Dashboard
`1805ebd7e046537f047d31212fdd5fda4454fed1` is unchanged. Existing repository
and dashboard checks, artifact preparation, deployment, live verification and
autonomous observation remain separate evidence. Automatic approval review
blocked private source export to the existing S3 release bucket and the image
build; neither operation was executed. Unaffected local and existing-repository
checks continue before any missing release authorization is requested.

Hosted CI then exposed two setup defects before tests ran: single quotes in
the PostgreSQL Docker health option were split by the runner, and the default
shell could hide the paired-source verifier's failure behind `tee`. The
workflow now uses the working double-quoted health command and an explicit
Bash shell with pipeline failure propagation. The intermediate corrected
backend `d51526f624f20116a1df2fb48ea6a71d48ba9bf8` collected 1,920 nodes, but
root interrupted it before completion for these required workflow corrections.
Its exit 130 is incomplete. The next full run must use the final clean source.

The subsequent `198bd45f75b6ddf7c81716977e3da17f5c617e88` diagnostic
collected 1,920 nodes and flushed 502 passing and two failing outcomes before
termination; it has no complete JUnit/summary and is not acceptance. One restore
case lacked the explicitly required PostgreSQL client-directory variable. The
other isolated Python child exposed user-site dependency leakage in the local
test interpreter. Both restore cases then passed with explicit PostgreSQL16
clients, and the isolated-child case passed in a self-contained pinned sibling
interpreter. Required acceptance now validates and records the exact client
directory and passes it to every child; hosted CI supplies its installed
PostgreSQL16 path. Tests and backup/restore assertions remain intact. The final
full run must use that self-contained interpreter and the clean published pair.

Dashboard PR browser setup stalled twice on the runner's Azure Ubuntu package
mirror before application checks. The final dashboard uses the existing
deployment's verified primary HTTPS mirror recovery and bounded dependency and
browser installation, retaining all required gates. Its two new mobile files
are exact latest-main contributions. Local Next development startup also made
an optional package-version query to the blocked npm registry. An explicitly
recorded local offline preload rejects only that exact update probe before
network access, exercising Next's normal unknown-version fallback; all
application and local HTTP requests remain real. The installed application
dependencies and repository source are unchanged by this environment setup.

The next reconciliation preserves concurrent backend commits through
`14bc99ebde648c5d77be47d9bd0bf516db9b1959`: existing loggers survive migration
initialization, connection identity retries retain their shared backoff without
granting read authority, and the disposable personality fixture supplies the
memory tables and PostgreSQL-compatible hash function used by the composer.
The dashboard retains the concurrent screenshot correction and zero console or
page-error assertions. Its import/content checks reopen the real Offers tab
after navigation; all existing assertions remain. The actual HTTP fixture now
declares its exact public origin, preserving the production CSRF checks.

A solo competing-worker test exposed a real claim race: account eligibility can
come from an older PostgreSQL statement snapshot while the next work query is
empty. Claim selection now rechecks within the same short transaction and
retains account order, scope and runtime-first locking. A deterministic database
barrier regression reproduces that race and checks both continued fair selection
and explicit account scope. Final complete verification remains pending.

Local full acceptance is coordinated across isolated whole-file commands on one
clean SHA pair. The complete committed manifest is collected once; recorded node
identities must partition exactly, with no omissions, duplicates, failures,
errors, skips or deselections. Timed capacity runs alone, while one serial
command owns the actual HTTP/rehearsal browser runtime. Both historical scripts
remain separate required commands. This local evidence does not establish the
separately required hosted combined/commerce workflows, image build or release.

Dashboard main subsequently advanced to
`18115d0a61550adefa1706eb67d551f0000eb1c0` with three render/layout commits.
The reviewed reconciliation preserves all 17 changed paths and the added CI
render gate. Controlled Vault/Offers tabs retain the hydration guard and mounted
drafts; native import, launch, multi-asset validation and durable upload controls
remain available. Browser interactions visit each control's actual visible tab;
existing content-sync assertions and upstream render assertions are unchanged.
The earlier `8e81944`/`aa6bae5` local collection contained 1,922 nodes, but no full
execution command started before this newer source was reconciled. That
collection is superseded rather than reported as completed acceptance.

The inherited render check now waits for the Campaigns component's explicit
settled state: its legitimate five-second polling cannot reach global network
idleness. All original assertions and timeouts remain. The unchanged desktop
media-position assertion exposed excess inherited Card spacing; a desktop-only
Vault row-gap correction restores first-viewport media without changing mobile
styles. The strict picker fixture now supplies only the two exact native sync
read contracts used by the retained mounted save component. Its assertions and
unexpected-request guard remain intact; no new write allowance was introduced.

Known main subsequently advanced through six commits to
`656216336f1f1c2c3415a6e85cf03ec9f698add0`. Its four-path reconciliation
preserves the exact upstream Users chat-header, Telegram desktop-height and
OnlyFans manual-reply placeholder changes. Send, takeover and pending-command
guards remain unchanged. The render helper retains every local and upstream
assertion, including both the original first-cover position and the stronger
complete-portrait-in-viewport check, plus explicit Campaigns/font readiness.
The upstream bounded cold-route wait and content-item readiness are retained.
Original full-run results must stay attributed to their original clean SHA pair;
successor validation, complete non-exception blob/mode equivalence, unchanged
node inventory and final artifact identities are separate required evidence.

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
