# Independent review: commercial preparation, 11 November 2026

## Verdict

**The implementation is on the agreed architectural path, but its preparation qualification needs three focused corrections before real onboarding. Commercial launch remains incomplete and also inherits the confirmed model-retry defect.** This is not evidence that the builder introduced the live no-reply incident: the affected model transport, scheduler and runtime are unchanged from the deployed base.

The reported tests are real evidence for their covered cases. They do not establish that every failure path works, that the full sales system is complete, or that production is ready. No review can certify zero future defects or prevent an external provider from rejecting a request. The required application behavior is bounded recovery, truthful status and preserved safety checks.

## Candidate and methods

- Work Server hostname verified: `ubuntu-s-4vcpu-8gb-syd1`.
- Project: `/srv/projects/twin-commercial-sales-20261111`.
- Backend: `7aa81dceff53b5ef4fc98feb085d25467e783d15`.
- Dashboard: `b77ec6d1eb6c9ae4ed6c3f54753dc0d804e8a969`.
- Image: `sha256:86108bebac6faa3634c012b6d493faee1eb231f207a59e5f95df10b2687d376a`.
- Base application: `659ea2913a7ea203024ea58a992f24b0fcea6759`; schema now `0132_account_preparation`.
- Both source trees were clean at initial inspection. Reviewed the change list against the base and traced preparation, connection binding, import, category review/pricing, shared delivery fences, dashboard/BFF and the inherited retry path.
- Compared 2,666 tracked files with the image: no missing files, no extra Python files under `/app/service`, and only `.dockerignore` differs. Application source matches. Evidence: `image-source-comparison.json` in the reviewer directory below.
- Inspected builder raw logs and commands: final-image shards **69 + 133 = 202 passed**. Inspected successful actual Next BFF -> ASGI middleware -> PostgreSQL integration, four TypeScript configurations, and 20 browser assertions. The browser used the actual Preparation component with synthetic fetch and minimal fixture CSS; this was not a production-dashboard acceptance run. I did not independently rerun those browser/BFF/TypeScript checks.
- Independently ran exact-image PostgreSQL checks and new counterexamples in a separate internal-network lab, with no application overlay: **77 existing checks passed and 3 new counterexamples failed**. First run: 21 preparation/provider-family regressions passed, all three new counterexamples failed (88.87s). Second run: 56 category/vault/automatic-description regressions passed (160.02s). These are subsets of the builder's coverage, not 77 additional independent product behaviors. Commands and full logs are retained.
- Review evidence: `/srv/projects/twin-commercial-review-20261111/`. Key files: `test_review_counterexamples.py`, `run-command.json`, `review-checks.log`, `regression-command.json`, `review-regressions.log`.
- No application edits, production access, deployment, paid provider/model calls, or fan messages in this review. The running trial and builder's databases were not touched.

## Findings requiring correction

### R0 — Inherited live model retry can strand a request (high)

**Confirmed previously against live records; unchanged in this candidate.** `service/model_transport/pilot_supervised.py:345-352` treats a released `http_429` call as uncertain. Selection at lines 364-365 consequently excludes its open request. `service/conversation_engine/model_runtime.py:209-216` supports a later invocation after cooldown, but the scheduler never invokes it. `service/model_transport/openrouter.py:206-207` retains only the HTTP status, losing the response's diagnostic and retry metadata.

The prior diagnostic report is `deliverables/PILOT-NO-REPLY-DIAGNOSIS-20261111.txt`. It records the 18:00:55 Brisbane inbound, released 429 call, and the successful new request after the fan's 18:13 follow-up. It establishes our scheduling defect, not the precise external quota or party that generated the 429. This review confirmed those application files have no diff from the deployed base; it did not re-query production.

Required proof: an actual supervised tick gets a classified retryable failure, persists a due time, restarts if needed, then answers without another fan message. Unknown outcomes, unresolved cost, identity holds and genuine refusals must not become retryable merely to make this test pass. Newer context must supersede stale work, not cause duplicate late answers.

### R1 — Preparation discards provider retry timing (medium; before real reads)

**Independently reproduced on the final image.** A mocked vault response with HTTP 429 and `Retry-After: 120` is immediately eligible for another pass. Two consecutive `prepare_one` invocations made two page reads about 0.049 seconds apart and reserved four requests total, including repeated identity reads. This direct-call probe demonstrates absence of a durable due-time check; it does not claim the production worker polls every 0.049 seconds. The ordinary loop defaults to 15 seconds and likewise has no provider-specific retry timing.

Paths: `service/fans_api_adapter/protocol.py:109-134,234-245`; `vault_sync.py:195-223`; `conversation_engine/preparation.py:240-244`; `onlyfans_worker.py:294-304`.

Test: `test_retry_after_blocks_early_second_page_read` fails. The budget ceiling still works; the defect can consume permitted budget on premature retries. Unlike R0, this importer DOES retry. Do not conflate the two mechanisms.

Fix in existing read/job owners: retain bounded sanitized status/trace/retry metadata, classify transient versus authentication/identity/schema failures, persist retry due time and attempts, and check due time before new identity/page I/O. Respect stop, identity, epoch and reservation checks. No independent second scheduler, no automatic budget increase, and no change to ambiguous send semantics.

### R2 — Failed preparation identity is hidden from saved dashboard status (medium)

**Independently reproduced on the final image.** An HTTP 401 identity response throws `Refused('provider identity could not be verified')`. Afterward `prep.view` still returns `status=configured`, `connected=false`, `vault=null`, with one request charged/reserved and no failure reason. The dashboard describes this as awaiting verification/worker activity.

Paths: `conversation_engine/preparation.py:146-168,240-242,296-305`; `dashboard/components/onlyfans-preview/Preparation.tsx:50-55`. The production loop catches the exception and returns a generic `held` result to its log; that cause is not persisted for the dashboard. This is not a false claim that identity succeeded, but the operator cannot distinguish a failed credential from work that has not started.

Test: `test_identity_failure_is_visible_in_persisted_status` fails.

Fix with R1: persist sanitized account-scoped attempt outcome, cause, last progress, retry due/exhaustion and required operator action. Surface those through the existing API/BFF. Treat missing credentials, authentication/identity mismatch, exhausted budgets and transient failures distinctly. Do not log secrets or blindly retry permanent failures. Preserve cursor and cumulative reservations across stop/restart/reconfiguration.

### R3 — Reanalysis can leave stale confident classification (medium; before classification use)

**Independently reproduced on the final image.** An asset first classified from bound metadata with confidence 99 is `proposed`. After its metadata confidence becomes 20, `assign_sync` returns `already_categorised` and retains the old `proposed / metadata_evidence` classification. No model or provider call was needed for this counterexample; it exercises the saved-metadata owner with the same style of fixture used by the builder's refresh tests.

Paths: `service/vault_auto_folders.py:101-123` returns at the one-time folder marker before checking current evidence. `vault_ai_api.py:2937-2946,3009-3029` retains those markers across description refresh. `onlyfans_runtime/vault_editing.py:150-152` derives `classificationUncertain` from that saved classification status.

Test: `test_later_uncertain_analysis_cannot_keep_proposed_classification` fails. Builder's `test_commercial_vault_pg.py:215-235` covers the first pending-to-analyzed transition, not this second analysis after classification.

This is NOT a demonstrated automatic sale or paid-media leak: drafts remain disabled, sale_approved stays false, and reviewed asset fingerprints provide another check. It is nevertheless inconsistent with the agreed uncertainty rule and can mislead later category review.

Fix: separate preservation of agency folder choices from freshness of commercial classification evidence. Recompute/invalidate classification for changed evidence, confidence or content stamp without undoing manual memberships, renames, archives or locks. Uncertain items must remain unsellable and visibly unresolved/Miscellaneous until reviewed. Do not simply clear every folder marker or silently reprice existing products. Test high-to-low, low-to-high, changed stamp, manual overrides and stale reviewed drafts.

## Proven protections worth preserving

- Preparation reserves permitted GETs before I/O; it does not call a planner or sender. Common delivery admission and policy enablement refuse preparation accounts, including stopped rows.
- Account/binding/connection/provider-family checks prevent native provenance from being silently reused as FansAPI proof. Identical media IDs across ten synthetic accounts remain separately scoped.
- Advisory locking prevents simultaneous preparation passes for the same account; unrelated accounts can advance. Apply-time checks reject pages returned after stop/revision changes. Cursors and cumulative reservations survive restart.
- Category drafts reuse existing catalogue/audit owners, require manual prices and reviewed assets, and remain disabled. Bundle totals are separate from single-category prices. Duplicate/conflicting batch commands are refused or replayed idempotently.
- Migration preserves the legacy pilot row and prevents a lossy rollback that would discard preparation authority. These are useful tests, not a populated operational restore/rollback qualification.
- Changed existing assertions inspected: schema pin moves to 0132; vault tests now retain metadata/history instead of deleting absent items. Retained media is not automatically sellable; shared readiness still checks ownership/provenance, readiness and selection. Do not restore destructive deletion merely to recover the older assertion.

## Evidence and release-record correction

The image still advertises inherited labels `plan-c.schema=0129_reconnect_receipt` and old `twin.overlay.*` values. The application actually includes schema 0132, and the file comparison above found no application mismatch. Correct these labels on the next frozen image and publish one authoritative manifest. Do not characterize this alone as a mixed-source deployment defect.

Older `BUILD-CHECKPOINT-20261111.md` describes the initial 3ce1a7e increment. The newer preparation report/candidate record supersedes it. Mark that reading order explicitly so a developer does not use the old source/schema as current.

## What remains for commercial completion

The existing v2 commercial plan still governs. This review does not authorize new product policy or a second engine.

1. Resolve R0 and R1-R3, with exact-image negative and positive controls. Use one owner per repair. The live retry patch must not require deploying the entire unfinished schema-0132 commercial branch to the trial.
2. Finish disabled preparation: approved credential references and creator identities/read ceilings; historical conversation and ownership coverage distinct from the latest-1000 catalogue; actual analysis and agency review. Unknown historical ownership cannot be treated as unowned. Qualify real reads only within separately approved budgets.
3. Add a compatible transition from retained preparation authority to the commercial account contract. Current rows deliberately cannot activate. Preserve identities, counters, audits and legacy pilot behavior; do not delete/recreate rows to bypass that design.
4. Complete the existing commercial owners together: relevant approved selection beyond the old 200-row assumption, singles and explicit mixed bundles, manual approved prices/discount terms, exact immutable card/receipt/unlock/access matching, staff context and voluntary tips. Prompts, validators, grants, edits and final dispatch must consume the same terms.
5. Prove the normal end-to-end sale without injecting a per-fan grant in the fixture: question -> accurate answer -> eligible offer -> one correct card -> matching unlock/access -> continued conversation. Include already-owned assets, old ownership, duplicate/out-of-order events, staff changes, stop/takeover, corrected input, crash, unknown send/cost and retry exhaustion; repeat isolation/fairness with ten accounts and shared fan/media IDs.
6. Freeze a final application/dashboard/schema/config manifest; qualify the actual dashboard build, provider/model behavior, populated restore/compatible rollback and emergency stop. Launch a bounded sale before expanding. Passing preparation tests alone does not authorize commercial launch.

## Review limits

This was a focused independent review of the changed preparation/commercial foundation and its integration with known failures, not a line-by-line audit of all legacy modules. No current production state, real provider payload, model quality, credential correctness, lifetime-history completeness or full commercial journey was proven here. No cross-creator leak or unintended send was observed in the inspected paths and rerun controls; that is evidence, not a guarantee about every possible execution.
