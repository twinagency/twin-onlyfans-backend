> Latest qualification continuation (2 November 2026): [QUALIFICATION-20261102.md](QUALIFICATION-20261102.md). The real bridge helper is now recovered and a fresh pinned local run is complete. PostgreSQL, matching sibling, Windows, real-model and production gates remain open. Earlier evidence below is retained as history.

# Verification and limitations

The current candidate continues integration parent `a6b93bd227fec82589f955593811543889194613`. Test evidence applies to this source candidate, not deployed services. No production database, provider account or customer conversation was used.

## Results

| Check | Result |
|---|---|
| Broad service sweep | 137 passing modules; 4 failed modules blocked by prerequisites; 139 PostgreSQL modules not run |
| Final authority regressions after the last repairs | 21 modules passed; production/test source hashes retained |
| Full-path commercial scenarios | 26 passed, repeated three times |
| Durable progress/control/migration tests | 43 passed |
| Synthetic prompt/guard evaluation | 320 fixture generations; no automatic hard failures; independent quality scoring not performed |
| Default 1,000-fan burst safety | 22 checks passed; 999 replies and one human takeover |
| Deployment / real-account delivery | Not performed; not ready for activation |

The four failed modules are `test_ledger_date_filters.py`, `test_twin_bridge_contract.py`, `test_twin_bridge_roundtrip.py` and `test_twin_bridge_runner.py`. Their prerequisites and release implications are described below. They remain failures in the machine evidence, not waived passes.

The 1,000-fan burst is a **10-second account-gap scenario** with virtual median 4,971 seconds and p95 9,435 seconds. It verifies retained work and safety while queueing. It does not satisfy a 180-second reply SLO at that load. The standalone final run passed with 165 seconds wall time; earlier 120-second harness limits timed out. Production concurrency/capacity requires its own PostgreSQL and provider-backed qualification.

## Evidence files

- `evidence/release-candidate-test-results.json`: broad sequential isolated service sweep, module status, exit code, elapsed time and test source hash. PostgreSQL modules excluded by the original runner are explicitly recorded, rather than counted as passes.
- `evidence/service-test-results.json`: earlier diagnostic sweep. It includes failures subsequently repaired and an insufficient 120-second limit for the default 1,000-fan virtual-clock burst. It is retained as history, not presented as the final result.
- `evidence/commercial-full-path.json`: 26 scenarios through actual NativeEngine, SellLane, resolver, ContextualAuthority, Gateway, receipt ledger and provider client, with only model and HTTP transports isolated. Sockets are denied. Includes unsuccessful and pending cases and separate first/repeat attempt distributions.
- `evidence/commercial-repetitions.json`: three successful repetitions of those 26 scenarios. These are fixture repetitions, not real-model generations or a conversion experiment.
- `evidence/*-run-meta.json` and `evidence/*-score.json`: 16 synthetic evaluation cases, five generations each, on both worker and FASTT prompt routes with clean and fault-injection profiles: 320 transport-fixture generations. All cohorts and hard-failure counts are retained. Zero automatic hard failures in these canned generations does not establish natural conversation quality; independent rubric fields remain unscored.
- `evidence/environment.json`: actual imported versions and runtime limits. Python 3.12 and pytest 9.2 were available here; the hosted pinned Python 3.13 / pytest 8.3.5 environment remains an additional CI qualification gate.

The last budget and settled-receipt pointer repairs were additionally retested in `evidence/final-authority-regressions.json`, including source hashes and full-path progress/protection checks. The broad sweep began before the final pointer repair; its results are supplemented explicitly rather than described as an entirely frozen-tree run. Historical `final-service-test-results.json` is an earlier 120-second diagnostic pass.

## What the new tests demonstrate

| Contract | Verification |
|---|---|
| Direct supported request bypasses warm-up | Real dispatch and receipt path at the start of a conversation |
| Proactive opportunity preserves the answer | Complete normal answer followed by exactly one paid card |
| Specific invitation, short acceptance | Confirmed invitation association, exact assets/amount, real paid dispatch |
| Ambiguous invitation | One bound clarification retains item/quote; the next yes reaches dispatch |
| Develop then fan request | Development produces no card; a subsequent explicit request reaches the original sender |
| Opportunity before/after reference | Real dispatch before the lower reference and after the upper reference |
| No current opportunity past boundary | No card; explicit no-offer disposition with resumption condition |
| Persistent generic explore at boundary | Becomes a verified item invitation without resetting counters |
| One extension; second attempt to extend | First is recorded; second is refused and reaches durable review |
| Purchase then different request | Confirmed ownership facts are seeded; only remaining assets are dispatched immediately |
| Rejection and ignored invitation | No follow-up card; invitation and attempt dispositions persist |
| Complaint, purchase coverage, missing inventory | Existing commercial authorities refuse paid dispatch while permitted chat continues |
| Edited source and takeover | Stale work cannot reach paid dispatch |
| Hold between plan and invitation send | Commercial hold is reread at the existing text wire boundary; nothing is posted |
| First bubbles vs repeat grouped replies | Durable state tests count confirmed rows, grouping, filler, campaigns, duplicate/replay and lifecycle events |
| Restarts, settings changes, extension expiry | State/authority tests preserve attempt identity/counters and require a valid terminal decision |
| Competing senders / multi-message evidence | Existing durable authority, concurrent-delivery, receipt/recovery and contextual evidence regressions |
| Off/shadow/live, adjustable settings | Real contextual paths and audited configuration persistence; shadow does not alter live attempt state |
| Legacy delayed sales | New live policy disables timer/payment-only jobs; separate on-request permission remains authoritative |
| Settled native pointer | Both durable receipt and attempt must be positive and scoped; no pointer or lease is cleared to allow own subsequent bubbles |
| Affordable budget | A supported request with a sufficient stated budget reaches delivery; over-budget and hardship remain refused |
| Quotes and recovery evidence | Frozen canonical amount/assets survive provider acceptance; existing receipt and lifecycle suites |
| Migration | Nullable additive 0108 upgrade twice, existing data retained, non-destructive downgrade on SQLite |
| Other products | Existing egress isolation, launch integration and complete-answer legacy prompt regressions |

The general hold, budget, refusal and recovery contract is also exercised by the existing service suites. PostgreSQL authority/locking behavior is not replaced by SQLite fixtures. There is no claim that every combination above has passed against PostgreSQL or a live provider.

## Genuine outstanding gates

- An owned PostgreSQL test server is unavailable in this environment. Bundled `initdb` refuses root, and the environment cannot create a nonroot uid. PostgreSQL modules, the two database-dependent cases in `test_ledger_date_filters.py`, migration locking and real multi-worker behavior are unverified here.
- The cross-language Twin bridge helper `service/tests/helpers/twin_bridge_server.mjs` is absent from the verified integration tree, and the sibling Twin repository/tsx dependencies are unavailable. Connected access to both current and historical sibling repository names returned 404. These gates were not replaced with fake adapters.
- `test_twin_bridge_runner.py` requires Windows `cmd`. Linux cannot verify its Windows batch exit handling.
- Claude's private revised execution plan, original incident transcript/case file and original unpushed workstation changes are unavailable. Supplied plan/handoff and accessible Git commits were inspected, but private F-row closure cannot be asserted.
- Repeated real-model generations, independent scoring, creator-specific language quality, live inventory readiness, production parity, production capacity, the two real-account delivery/unlock checks and commercial lift remain unverified. Existing R9 approval gates remain mandatory.
- Existing post-401 cooldown remains 300 seconds, as documented in the handoff. This continuation does not change authentication policy.

The experimental windows are configurable defaults for offline review, not conversion-optimised claims. Explicit numeric USD budgets are enforced by the canonical path; semantic interpretation of open-ended budget language still depends on the existing planner/hold policy and requires the private transcript and real-model evaluation.
