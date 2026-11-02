# Recent-history onboarding, 2 November 2026

Release status: **draft, not deployed, per-account rollout off**. Maile end-to-end qualification is blocked by lack of access to the operational OnlyFans PostgreSQL host. There is no demonstrated 90-minute onboarding result or measured speedup.

## Source integration

The dedicated branch is `codex/recent-history-onboarding-20261102`.

- Original/concurrent-release parent: `d3a702262abd918588587c12f67c5fb9611100d7` on `codex/of-original-completion-20261102`.
- History/capacity/memory parent: `e3f033c3bffcd037696a82e79ef50ccc86e939c6` on `codex/parallel-history-capacity-20261101`.
- Shared base: `7616ec92bf01a6b7f3beb45bd45b943309c17285`.
- Both remote heads were reread before publication and remained unchanged. A three-way integration retains the capacity governor, adaptive page sizes, error classification, memory quality and billing repairs alongside the original release's bounded-starvation read lane. Existing capacity settings and daily model caps are unchanged.
- The two historical migration lines have overlapping numeric prefixes. A real Alembic merge revision, `0109_history_release_merge`, joins them; `0110_recent_history` follows. Schema checks now use migration ancestry as well as rank. Both populated upgrade paths are tested.
- The separately running concurrent-reply repair checkout and its unfinished files were not changed. This branch does not include those uncommitted edits. Reconcile its subsequently published commits before any release. No sender, conversation engine or Moxy redesign is added.
- The last **documented**, not freshly inspected, production overlay is recorded in `deploy/memory-accounting-20261102/README.md`: worker `sha256:f728669d70d9fcb89e34ced38e7efe808311293d0b175422b004a46114284fe5`, API `sha256:04532a2d90349893407c08c77a883a6cc62c9ed8386d4eb29b21ab3fc9ef9975`, schema `0106_parallel_history_reads`. Current production source/image/schema still require a host check. Do not deploy a whole older branch over the running engine.

The dashboard companion branch has the same name in `twinagency/twin-dashboard`, based on `86691f86b06baacf4a1b058f79bf4dd529f07d12`. Its strict connection parser must land before enabling the new response on an account. The new card separates recent coverage, memory processing, older jobs, lifetime completeness, charges and reservations. No lifetime percentage is invented. Browser rendering and production dashboard deployment remain unverified.

## Behavior

The deployment allowlist `ONLYFANS_RECENT_HISTORY_ACCOUNTS` defaults to `[]`. A current-binding durable policy freezes the cutoff when onboarding/import first starts. Reconnecting or restarting preserves it. It is an import boundary, never a deletion rule.

Independent recent scan and per-fan cursor tables leave all existing lifetime cursor positions, messages, receipts, purchases, memories, manual corrections and reply controls intact. The initial import saves both incoming and outgoing events inside the window, including the existing canonical media metadata. The small live-head bootstrap uses the same cutoff. Every thread is independently traversed. Old threads cannot stop discovery of other fans.

Discovery uses full offset sweeps and requires identical ordered identity sequences on two complete passes, independent of page partitioning. After stable discovery, completed threads are scheduled for another head catch-up. Per-thread import also has a durable initial/head-catch-up cutover. Applied canonical message identities deduplicate overlap with live intake. Claims, pagination, backoff, retry state and processing receipts survive restarts. Invalid, inaccessible or interrupted pages do not complete their cursors.

### Provider ordering limitation

The current connector requests `order=desc` and validates descending message IDs. That is **not proof of descending creation timestamps**, and it exposes no implemented date filter or stable snapshot cursor. Therefore the safe default is exhaustive ID traversal with date-filtered persistence. It can reduce model extraction work, but it may still need the full provider traversal and adds discovery/catch-up overhead. It does not prove faster download completion.

Date-based early stopping is enabled only with an explicit verified-order flag plus recorded ordering evidence. Incoming pages must remain timestamp-descending, including across page boundaries; violations fail closed. Merely seeing one old message never enables the optimization. The operator must verify the real adapter/provider contract and record evidence before selecting it. Offset discovery remains subject to the provider's available, accessible conversation list; inaccessible threads remain blockers. Continuous list churn can prevent the required stable passes from completing.

Previously completed, validated lifetime coverage can terminate overlapping recent reads once that stored coverage is reached. Older stored sources remain available immediately. Activated older jobs wait for eligible stored older extraction receipts before requesting further older pages when the learner is enabled. Unknown gaps are not bridged by advancing to an arbitrary minimum stored ID.

### Memory and creator knowledge

Both the paid `profile_learning` queue and deterministic `launch_history_memory` processor respect the fixed cutoff and activated older jobs. The paid learner retains relevant preceding outgoing context, bounded batches/chunks, and fingerprinted receipts, including valid fact-free results. Old jobs and manual corrections remain stored. Recent sources precede activated old sources within a paid extraction batch. Scoped-processing completion is explicitly distinguished from all stored history.

The existing immutable personality review/approval workflow accepts five separate onboarding sections: fan-facing biography/talking points, private information, writing style/examples, boundaries/approval requirements, and pricing/operating notes. Only approved public sections enter native and FASTT prompts. Private information is excluded. Current catalogue prices, eligibility, ledger checks and runtime controls retain authority; narrative pricing notes cannot grant permission or override them.

Routine creator archive extraction is disabled for opted-in accounts. Existing creator-profile queues remain in place. An explicitly configured writing sample of at most 100 outgoing messages can be used through the existing review workflow when writing examples are missing. Samples remain drafts requiring approval. Placeholder text and mixed PG/PG-13 plus explicit/sexting instructions are flagged and block approval. This conservative linter is not a semantic proof that all possible conflicting wording has been caught. Existing approved configuration remains the runtime authority.

A live inbound message activates one durable older-history job per account/fan pair. Repeated triggers preserve cursors and can raise retrieval priority for prior references, promises or missing payment context. These priority hints never establish payment. Existing bounded provider admission, cooldowns and account model caps still apply. One periodic older turn and aging prevent ordinary queue starvation within available configured capacity. Live fan work retains its existing priority. The engine is told to use only available evidence and to take the existing verification/review path for unavailable payment, access or fulfilment evidence. Older completion is not promised before the first reply.

## Measurement and rollout

See `MAILE-MEASUREMENT.json` for honest current availability and `evidence/` for isolated test results. Maile is the first intended reference account, `of-323358988`.

1. On the operational host, first record current source/image/schema and reconcile the active concurrent-reply release. Capture canonical counts, receipts, purchases, enabled/muted/takeover settings, caps and read capacity. Rehearse the actual current schema upgrade and check role/RLS behavior on a disposable copy. New recent-history tables have RLS enabled and no public/anon/authenticated grants; existing server authority remains in use.
2. Integrate the two reviewed code lines and the dashboard companion. Drain import/learner work before the schema upgrade. Apply the Alembic merge and additive migration; retain the existing separately provisioned `deploy/profile-learning/schema.sql` tables and receipts. Do not start a reply supervisor or enable any replies.
3. Keep the existing capacity map, rates, concurrency and cost caps. Set the allowlist to **Maile only** on the cooperating API/importer/learner/worker services. Existing accounts use the first cutover time as the fixed initial-import start; the original archive progress is not reset. New accounts freeze at connection/import bootstrap.
4. Keep `timestamp_order_verified=false` until real provider ordering evidence has passed review and testing. The exhaustive fallback must be reported honestly.
5. Before and during the run, use the read-only report with the existing worker role/tenant/database configuration:

   `python -m tools.history_onboarding_report --account of-323358988`

   Save before, during and after outputs privately. Compare current-binding stored source volume and receipts; record transport-attempt counts, elapsed import time, actual provider-reported model charges, estimates, active/unresolved reservations, memory receipt timestamps and cumulative model latency separately. Unknown lifetime counts remain null. Returning-fan processing can contribute to account memory cost after policy start and is labelled accordingly.
6. Declare recent readiness only when discovery and every recent thread succeed. Memory readiness is a separate receipt/error/budget check. Compare complete, comparable runs or appropriately bounded stored-workload counts. A source-character fraction is not a measured token, cost or time saving. Expand only after Maile passes coverage, control preservation, purchase verification and budget gates.

### Rollback

Remove the account from the allowlist, or explicitly disable its durable policy, on all cooperating services. Drain in-flight scoped work first. Keep the integrated schema-compatible binary while using the legacy lifetime scheduler. All data, receipts, recent cursors and old cursors remain available. The migration deliberately refuses a destructive downgrade. An old schema-HEAD-checking binary is **not** an image-only rollback from `0110_recent_history`; prepare a schema-compatible rollback build. Turning the flag back on reuses the original cutoff and progress.

## Remaining gates

- Current operational host/image/schema inspection and Maile live run.
- Real provider timestamp-order proof before date-based early stop.
- Actual Maile recent-window fan/message counts, request counts, import duration, memory duration, charges and full-history comparison.
- Dashboard browser rendering, deployment and integrated UI checks.
- A stale ledger reconstruction test also fails on the untouched original baseline; it attempts to drop an already absent `daily_limit_units` column. Its fixture is not repaired in this scoped change. Keep its result explicit when evaluating whole-release readiness.
- These checks do not qualify the separate concurrent-reply release or remove its existing R9 gates.

## Executed verification

- Broad isolated PostgreSQL run: **211 passed**, two PostgreSQL-only capacity cases initially skipped, 249.53 seconds. This candidate preceded small discovery-digest, priority and reporting refinements.
- Final targeted PostgreSQL run after those refinements: **56 passed**, 70.94 seconds. Includes all 16 new recent-onboarding cases, the approved-onboarding/native/FASTT checks, both current release-line upgrades, and the two read-lane fairness suites.
- The two skipped capacity cases were then executed on real isolated PostgreSQL: **2 passed**, 1.07 seconds, covering simultaneous admission and migration/cursor preservation. Counts overlap; they must not be added as a distinct-test total.
- Dashboard strict-parser checks: **12 passed** on Node 24. Rendering in React/a browser remains unverified.
- The original ledger reconstruction failure was separately reproduced on the untouched original source baseline: **1 failed**, 6.15 seconds. It remains a whole-release qualification issue, not a claimed pass.

The evidence directory contains raw logs, limitations, and hashes of final relevant source files. All records are synthetic. No live fan messages were sent and no paid model calls were made. Test runtime does not measure Maile onboarding.

GitHub publication check: both draft PRs are cleanly mergeable and both source parents are retained. The newly triggered backend runs `41387702674` / `41387666936` and dashboard runs `41387741369` / `41387736177` ended in `startup_failure`. No GitHub CI pass is claimed; this is an additional release blocker. Local PostgreSQL/contract evidence remains separate.
