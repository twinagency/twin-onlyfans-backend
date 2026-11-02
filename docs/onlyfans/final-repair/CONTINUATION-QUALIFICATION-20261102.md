# OnlyFans continued release qualification, 2 November 2026

**Not release-qualified.** The code repairs and final isolated measurements are preserved. This continuation performed no merge, deployment, production migration/configuration, activation, purchase or customer send. Moxy remains outside this release.

## Exact candidate and preservation

Branch: `codex/of-original-completion-20261102`. Final application/test source: `1b4bbf646adab11e5cdfee4eff25b772cee37dc9`. Runtime repairs are in `5760c3cfedb9893e06862903bd50d99bc0b58cd5`; the later source commit only fixes the ownership-race test wrapper and adds the operator runbook. Subsequent evidence commits do not change tested runtime/test bytes. Parent `766a7992908e5706d024b90e083d6a260f93a8c5` was re-read before expected-head updates. No branch history was force-pushed.

The old checkouts and laptop repair4 snapshot survive. Their source hashes were compared; recovered historical fixtures and identity/authentication assertions were retained. The local mirror has reconstructed Git ancestry, so it was not pushed as remote ancestry. The remote tree preserves original executable modes and omitted desktop/image blobs. Every one of the first 27 changed remote blobs matched the tested local content exactly. Final source hashes accompany this report.

## Repairs and scope

* Recovered legacy dashboard configuration compatibility using explicit field negotiation. Added strict opt-ins for newer vault continuation and folder-cover fields, preserving older dashboard validators, current account scope and command authority.
* Reused one schema-validated control Store/pool per account rather than recreating it for every fan safety scan. Ownership, holds, pricing, purchases and final wire authority still read current rows. A multithreaded regression proves children share one store and a changed target is refused; the owner closes its pool at shutdown.
* Added the existing configuration API's `nativeReplyDelayMaxSeconds` field, accepting 90 or 180, defaulting to the existing 90. Scheduling remains based on the original received input. Populated PostgreSQL and SQLite checks preserve stored schedules and widen only the known constraint; unknown definitions are refused.
* Restored historical-schema fixtures using their actual historical fields. Updated identity/authentication expectations to assert durable holds and real authentication behavior. Unsolicited false identity claims still have an explicit post-generation refusal test.
* Corrected the test/demo public-asset copy for the paired dashboard's shared public directory. Retained a test-only browser assertion patch for the fixture's missing verified provider connection.
* The broad diagnostic found an ownership-race wrapper missing the concurrency API's `io_offload` keyword. Forwarding it to the real wait preserves all barrier, takeover, reservation, exactly-one-POST and exactly-one-attempt assertions. Its focused full script passes. No production coordination code was weakened to satisfy the test.

## Complete final service run

The frozen final run completed **285 service modules: 282 passed, 3 failed**, with source unchanged. Skipped/empty-module scan: **0**. Ordinary independent modules ran in three isolated processes; timing, capacity, browser and native-service modules ran sequentially afterwards. Tests used scrubbed credentials, disabled dotenv, the loopback Python socket guard, separate owned databases and retained exit codes/logs. The supplemental managed-proxy suite passed **36 tests** on its own disposable cluster. Its first attempt could not reach a different execution session's loopback server; that setup failure is retained separately, then replaced by an owned-server rerun, not called a product pass.

The 3 final failed modules remain failed in the complete Linux report. The Windows runner separately passed natively on `LAPTOP-PKFJ7M3I`, exit 0, with both its test and command-runner SHA-256 hashes exactly matching the final candidate. That clears its platform-specific requirement without relabelling the Linux run:

| Module | Cause / required resource |
|---|---|
| `test_twin_bridge_contract.py` | Matching real Twin sibling/channel adapter and locked tsx dependencies unavailable; test not executed. |
| `test_twin_bridge_roundtrip.py` | Matching real Twin sibling/channel adapter and locked tsx dependencies unavailable; test not executed. |
| `test_twin_bridge_runner.py` | Cannot execute cmd on Linux. The same test and command-runner bytes passed on native Windows, exit 0; keep this Linux row failed. |

The interrupted preceding diagnostic is retained: 267 modules completed, 263 passed and four failed, including the ownership fixture subsequently repaired. It is not an all-green frozen result. Final results, command lines, hashes and compressed logs are in [the evidence index](evidence/release-continuation-20261102/index.json).

The final run retains the 43 commercial-state tests, 26 full-path scenarios, concurrent delivery, quote/purchase/receipt persistence, history/migration, holds/takeover, egress and account-isolation checks. The 800 native PostgreSQL service checks and 776 local service checks are individual modules, not substitutes for the complete run. Synthetic HTTP never establishes actual-model quality or provider-supported capacity. The paired browser uses restored `ce99d8a` source plus the disclosed assertion patch; this is not exact parity with the current onboarding dashboard.

The prior frozen PostgreSQL sweep's 17 failures retain their historical status. Their outcomes in this final Linux run are:

| Previously failed module | Final run |
|---|---|
| `test_native_pilot_load_pg.py` | PASS |
| `test_onlyfans_content_sync_delivery_pg.py` | PASS |
| `test_onlyfans_http_pg.py` | PASS |
| `test_onlyfans_ledger_history_pg.py` | PASS |
| `test_onlyfans_loopback_transport_pg.py` | PASS |
| `test_onlyfans_media_upgrade_pg.py` | PASS |
| `test_onlyfans_native_connector_pg.py` | PASS |
| `test_onlyfans_native_memory_pg.py` | PASS |
| `test_onlyfans_native_planning_pg.py` | PASS |
| `test_onlyfans_native_upgrade_pg.py` | PASS |
| `test_onlyfans_provider_pg.py` | PASS |
| `test_onlyfans_recovery_pg.py` | PASS |
| `test_onlyfans_release_rehearsal_pg.py` | PASS |
| `test_onlyfans_sync_retry_pg.py` | PASS |
| `test_onlyfans_vault_media_pg.py` | PASS |
| `test_onlyfans_worker_entry_pg.py` | PASS |
| `test_r3r7_native_planning_pg.py` | PASS |

Environment: Python 3.12.14 and pinned qualification dependencies, PostgreSQL 16.15, Linux, task-owned tmpfs clusters and the previously retained test-only UID ownership shim. SQL, locks and transaction code were real. This is not immutable production-image, OS-user, persistent-storage/fsync, real WebSocket ingress, Python 3.13 or 50-production-container parity. Each owned final server was stopped after its checks.

## Measured performance, not a live capacity claim

Same-host code comparison: 100 fans, concurrent mode, preparation 8, source burst 5, model 0.2s, provider POST 0.12s, read 0.02s, no artificial pacing. At rate 1/send 4, baseline 106.776s became 99.75s. At the **separately synthetic rate 10/send 8**, baseline 99.873s became 20.886s. Within each pair, allowance and workload match; do not compare the rate-1 baseline to the rate-10 candidate as a code-only gain.

The rate-10 run reduced full schema validations from 101 to 1, and safety-scan p95 from 6.2492s to 0.0170s. Its pre-model context/ownership p95 fell from 7.7626s to 1.3967s; pickup/slot p95 from 90.1818s to 19.5275s; process CPU 94.1s to 17.164s, and peak connections 31 to 18. The 20-fan profiles also show repeated reflection falling. These overlapping stage totals are not additive end-to-end latency. Preparation 12 and 16 were slower than 8 and were not selected.

Final exact-source load runs, performed after the final service sweep with no competing qualification job:

| Run | Confirmed first replies | Full replies | Elapsed seconds | First-response p95 seconds | Share within 180s |
|---|---:|---:|---:|---:|---:|
| frozen-100-rate1 | 100/100 | 100 | 100.099 | 95.0318 | 1.0 |
| frozen-100-rate10-diagnostic | 100/100 | 100 | 28.511 | 27.8829 | 1.0 |
| frozen-3x20-concurrent | 60/60 | 60 | 24.826 | 22.9839 | 1.0 |
| frozen-50x2-concurrent | 100/100 | 100 | 61.632 | 55.6333 | 1.0 |

The 20-second target is **FAILED**. The final diagnostic records 65/100 complete replies within 20 seconds. This uses every eligible turn and full-reply completion, not a successful-only first-reply percentile. The previous 20.886s diagnostic remains a failed historical run. Rate 10 is not a verified native provider allowance and must not be installed from this test.

Full 25–180 pacing was exercised using the new setting. Retained simple paced runs completed 100/100 on one creator by 169.991s, 60/60 over three creators by 173.963s, and 100/100 across 50 creators x 2 fans by 167.983s. The heavier single-creator multi-bubble/media workload is **FAILED**: 85/100 substantive first replies within 180s, 92/100 by the 195.019s cutoff, 8 outstanding, p95 of confirmed first replies 187.2223s. Unfinished turns stay in the denominator. That heavy run's application and benchmark bytes match this candidate; the sole subsequent Python difference is the separately repaired ownership-test wrapper. No easier workload is substituted for it.

The shared rate-1 budget, pacing and multi-bubble completion/fairness remain material. Keep the failed overall timing gate visible. The 50-creator simulation has only two fans per creator, real Services in one process, and a declared shared async pool of 64 under a 300-connection test server. It does not prove 100 fans per creator or a production fleet connection/memory budget. Keep the 300-second 401 cooldown and unresolved-delivery holds; neither is a latency pass. Existing 429, receipt-loss, takeover and crash/recovery results remain in the accepted concurrency report with their original limitations.

## Refreshed live baseline, read-only

The read completed at `2026-11-02T13:31:40Z` (23:31 Brisbane), SSM command `b8ad119e-1948-4d27-ac5c-3d3b1d9d293a`, response 0. Schema is `0110_recent_history`. API image is `sha256:87bc296eb275e2c220c3daa2726d34c5e9d8c4faa77455b5bf7fd77e8c00f439`; worker/importer/learner image is `sha256:6afd1ad6bfc1030cbb0c7cd72d675753cde487b9de64bf0e333b02d89b4831f2`. They are labelled `9d6756b20063dc7d481f59acfb1f26c65336c357`. An earlier successful read found `91c260b`: other work updated these roles during qualification, and this continuation preserved both changes. The supervisor remains created, never started, on its earlier 596087b image.

All eight saved delivery policies use gap mode with 20-second spacing. All eight new-conversation automation settings are false. The sampled configuration/runtime/connection revisions and conversation counts are read-only evidence, not approval to change them or proof of effective per-fan sending eligibility. Six ledger scans were idle with recent completion timestamps; two remained running, one with an old completion and provider-read deferral, the other without a completion. Two accounts had no live-vault aggregate rows. Description totals do not establish sale-ready versions, previews or purchase coverage. No current release readiness is inferred from these aggregates.

The five current worker file hashes were checked in the authorised repository and remain unavailable there. Four match the previously captured missing blobs; the profile-learning worker has advanced to `a0ef73b23cb6107c9fc453d36ff1f79c5db3612b`, also unavailable. The current schema blob is available. The recorded hashes, not yesterday's worker file or an image label, govern preservation. No worker source contents were exported from production. This clears the stale-access issue, while complete source/image parity and a reproducible compatible release remain blocked.

## Actual-model evaluation and cost

Actual generations: **0**. Actual evaluation cost: **US$0**. The prepared full-context sample contains 200 distinct cases; each route has 120 case-route evaluations including crossover, with three planned generations and bounded repair. The existing approximate planning estimate is US$1.74 combined under the US$2 total cap, using an explicitly unverified price seed. It is not a current billing quote or a completion guarantee. Pre-request conservative reservations, both routes' shared durable journal and authoritative cost settlement remain required. Never reset pending/ambiguous cost to resume.

Neither the cloud process nor the laptop process had the named evaluation credential configured. Approved case expectations, creator facts/catalogue/quotes/currency/purchases/delivery/holds for the original historical cases also remain unavailable. The public revision-2 execution brief was located and its R9 section read; it does not contain the private world-state file or exact delivery tuples. No canned output was promoted into a quality score. Required completeness, relevance, factual/content/price truth, direct request and invitation acceptance, commercial progress, refusal/budget/hold, repetition and over-suppression scoring therefore remain blocked.

## R0–R9 disposition

| Gate | Isolated evidence | Failed or blocked release work |
|---|---|---|
| R0 | Branch/remote trees rechecked; saved repairs reconciled; fresh role/image/schema/settings/hash read; migration graph comparison and populated 0108-to-0110 rehearsal retained | Complete reproducible source/image parity and preservation of five unavailable current worker files |
| R1 | Final hold, takeover, ownership and recovery checks; ownership fixture repaired without weakening assertions | Named staffed operator, actual acknowledged handover; real sibling bridge integration |
| R2 | Complete-answer/guard/format regressions | Repeated actual-model completeness, relevance, repetition and over-suppression review |
| R3 | Facts, attachment states, durable identity cases and false-claim regressions | Authoritative private case facts and actual attachment/operator evidence |
| R4 | Inventory/read fairness/media checks | Current real shelf descriptions, versions/previews and provider-read/ledger readiness |
| R5 | 43 state tests and 26 full-path scenarios; canonical authority retained | Actual-model opportunity/acceptance/progress judgement and commercial outcomes |
| R6 | Quote/receipt/purchase/recovery and populated upgrade checks | Reconciled deployed branch/image and approved real unlock/purchase proof |
| R7 | Intent classification and legitimate-neighbour regressions | Actual-model incident and nearby legitimate-case scoring |
| R8 | Final declared loads, pacing configuration, bounded concurrency and retained fault/recovery tests; 20-second diagnostic: FAILED | Heavy 95%-within-180s target fails; native allowance/live capacity and full crash-under-load qualification remain unproved |
| R9 | Exact-source review, evidence and [migration/configuration/recovery runbook](CONTINUATION-RELEASE-RUNBOOK-20261102.md) prepared | Preceding gates, immutable compatible image, exact two approved account/fan/media/price tuples, explicit delivery/unlock permissions and staffed bounded pilot |

## Migration, configuration, recovery and the two live checks

Use the linked runbook. It preserves both Alembic branches through 0109 and 0110; the original candidate's 0108 schema code cannot replace an 0110 deployment. The populated upgrade preserved 155 original tables, unknown/accepted intents, stopped runtimes and proxy evidence, and was idempotent. The 26 latest onboarding/scheduler tests and read-only 0110 recovery API check pass on the separate compatibility source. That source is not an immutable release/rollback image and does not establish deployed source parity.

The earlier fceb624 read-only recovery source passes on 0108 but fails on 0110. Do not install it on the newer schema. No stamping away revisions, generic bootstrap of populated data, obligation deletion, unknown-receipt replay or overwriting unpublished worker work is permitted. Existing 90-second pacing remains the default; the 180 setting and every pilot delivery/commercial change await separate approval.

The two R9 checks are the original two **approved account/fan/asset/preview/amount/currency tuples**, not arbitrary replacement fans. Their exact values remain missing. For each, after approval, prove one dispatch intent, provider acceptance, local projection, visible exact card/price/assets, approved unlock, purchase history/ownership and duplicate-free reconciliation. Verify acknowledged human handover. Only then review a single-creator, explicitly bounded staffed pilot; the runbook prepares a maximum ten approved fans/60-minute engineering observation, subject to matching isolated load qualification and explicit approval. It is not activated and is not commercial proof.

## Smallest remaining resources/actions

1. The deployed worker's five missing source overlays in the authorised repository: `service/native_pilot/budget.py`, `service/onlyfans_read/earnings.py`, `service/onlyfans_runtime/profile_learning/evidence.py`, `service/onlyfans_runtime/profile_learning/worker.py`, `service/onlyfans_worker.py`. Current read-only hashes, including the newer profile-worker blob, were checked and still return 404. Their owner committing the exact current source is the smallest alternative to the previously rejected production-source export. Preserve the observed 9d6756b work before image preparation.
2. The matching Twin sibling with its real channel adapter, configuration and locked dependencies; set `TWIN_ROOT` only to that verified isolated checkout. No stand-in adapter clears the two integration failures. The native Windows runner proof is now cleared separately with exact unchanged test/runner hashes.
3. A securely configured evaluator credential and permitted OpenRouter route, the existing shared US$2 journal state, and reviewed world-state/rubric for the original cases. Do not paste a production secret into the report or invent catalogue/purchase facts.
4. A reviewed integrated 0110 source, exact role/recovery images and populated rehearsal based on current read-only evidence, preserving newer onboarding/worker work. GitHub Actions also requires its pre-job startup problem resolved; the inspected candidate run had startup_failure and zero jobs, not a test result.
5. Resolve the heavy timing failure for the declared load or explicitly review a newly qualified bounded operating scope; verify the native allowance and fleet connection/memory budget without raising a simulated limit and calling it provider approval.
6. The original two R9 tuples and named staffed owner from the private plan/release operator, followed later by explicit deployment, delivery/unlock and pilot approvals. Those actions were prepared, not performed.
