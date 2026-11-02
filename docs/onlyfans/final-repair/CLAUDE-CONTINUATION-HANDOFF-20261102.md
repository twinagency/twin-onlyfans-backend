# Claude handoff: finish the original OnlyFans release

Jesse requested this handoff on 2 November 2026 and asked the current agent to stop implementation and push the work. The current agent has stopped implementation. No live release or customer-send permission is implied by this handoff.

## Objective and boundaries

Finish the **original OnlyFans release qualification**, preserve the concurrent-reply repair, and prepare a reproducible release that retains newer onboarding/history/worker work. Complete permitted implementation and isolated verification, then report every failed or blocked gate honestly.

Jesse's latest constraint: do only what this task requires. Do not add speculative features, unnecessary abstractions, unrelated refactors, extra senders/queues, new product behavior, or repeated experiments seeking a nicer number. Do not stop after routine commits/tests to ask whether to continue. Finish independent work and escalate only a specific missing resource or authority.

Authorised: implementation, isolated testing, read-only deployment/schema/configuration inspection, and publishing the review work to this repository/branch. **Not authorised: merging a PR/default branch, deployment, production migration/settings changes, account activation, purchasing/unlocking content or customer messages.** Prepare those actions for later explicit approval. Do not stop live services, undo other chats' work or clear intentional pauses.

Keep the **Moxy redesign outside this release**. The separate 30-day onboarding/history work must be preserved and checked for compatibility. Telegram and FLIRT must retain their behavior. Use existing conversation, seller, quote, ownership, hold and delivery authorities.

## Repository and immutable checkpoints

* Repository: `twinagency/twin-onlyfans-backend`, verified private.
* Review branch: `codex/of-original-completion-20261102`.
* Incoming verified remote head: `766a7992908e5706d024b90e083d6a260f93a8c5`.
* Accepted concurrency implementation: `cfab868e2c87706ddce4ff6cfb2bbeffd76c720f`, parent `d3a702262abd918588587c12f67c5fb9611100d7`; 766a799 records its final evidence.
* Continued runtime/compatibility repair: `5760c3cfedb9893e06862903bd50d99bc0b58cd5`.
* **Final tested application/test source: `1b4bbf646adab11e5cdfee4eff25b772cee37dc9`.** This adds the ownership-test wrapper fix and runbook after 5760c3c; it does not add another runtime repair.
* The subsequent publication containing this handoff and qualification evidence changes documentation/evidence only. Its application/test bytes remain those of 1b4bbf6. Re-read the actual remote head and preserve any subsequent valid work. Use expected-head updates, never force-push another worker's changes.

The cloud checkout has reconstructed local ancestry. Its local commits are **not** authoritative remote ancestry. The previous agent published trees with the actual remote parent and verified the changed blob hashes. Do not push the reconstructed local branch blindly over GitHub.

## Read these first, in this order

1. [CONSOLIDATED-IMPLEMENTATION-BRIEF.md](CONSOLIDATED-IMPLEMENTATION-BRIEF.md): authoritative consolidated R0–R9 behavior and R5 clarification.
2. [CONTINUATION-QUALIFICATION-20261102.md](CONTINUATION-QUALIFICATION-20261102.md): final results, measured capacity, live baseline, all acceptance statuses and resources.
3. [CONTINUATION-RELEASE-RUNBOOK-20261102.md](CONTINUATION-RELEASE-RUNBOOK-20261102.md): graph, configuration negotiations, recovery and R9 approval boundary.
4. [Evidence index](evidence/release-continuation-20261102/index.json), especially `service-results.json`, `former-pg-failures.json`, `capacity-summary.json`, `live-readonly-summary.json`, and `windows-runner.json`.
5. [CONCURRENT-QUALIFICATION-20261102.md](CONCURRENT-QUALIFICATION-20261102.md): accepted concurrency design, earlier workloads/fault results and their limitations.
6. [QUALIFICATION-CHECKPOINT-20261102.md](QUALIFICATION-CHECKPOINT-20261102.md), [ISOLATED-QUALIFICATION-UNBLOCK-20261102.md](ISOLATED-QUALIFICATION-UNBLOCK-20261102.md), and [R9-REVIEW-AND-RELEASE-GATES.md](R9-REVIEW-AND-RELEASE-GATES.md): historical qualification, rejected transfers, operational gates. Their older deployed version/source hashes are superseded where the new report explicitly says so.

Earlier original references are Claude integration `a6b93bd227fec82589f955593811543889194613` and evaluation harness `64b6e802dae357609868cf80cbda1d64ebf7a83d`. The private revision-2 `OnlyFans_Claude_Execution_Brief_2026-11-01.md` was recovered and its current R9 section read. The separately referenced `OF-R0-R9-EXECUTION-PLAN-2026-11-01.txt` with real IDs/world-state remains missing. Do not confuse the public consolidated brief with that missing private authority.

## Full behavioral scope to retain

| Stage | Required outcome |
|---|---|
| R0 | Reproducible source/image/schema/configuration baseline; preserve newer valid work and intentional pauses. A branch or image label alone is not deployed-source parity. |
| R1 | Shared action-aware holds, durable cases, ownership and acknowledged human takeover across existing senders. Serious distress, consent, identity and general stops retain appropriate scope. Hardship/complaints suppress commercial pressure without unnecessary suppression of permitted support. Cases need an actual staffed owner. |
| R2 | Complete, relevant answers to the whole current fan message; no forced fragments, interview/nickname loops or obligatory questions. Split at sentence boundaries and cancel stale tails. Retain bounded repair, placeholder/identity/price/media-claim guards and visible review on persistent invalid output. |
| R3 | Approved creator facts outrank history/inference. Unknown stays unknown. Keep provenance, photo subject/identity separation and attachment states. Do not rate unsolicited images, request already received attachments, invent custom availability or promise unsupported fulfilment. Identity questions pause/flag rather than claim either AI or human identity. |
| R4 | Real described inventory, valid asset/version/preview evidence, accurate product/quantity/type/price, purchase coverage and bounded shared provider-read fairness. No invented catalogue or extra provider workers. |
| R5 | Existing evaluator/planner/SellLane chooses verified relevant offer, a specific real-item invitation, or natural topic development. Direct requests/clear acceptance progress without needless requalification. Durable first/repeat attempts, confirmed counters, at most one necessary extension, explicit no-opportunity/hold reasons, refusal/ignored/purchase memory. No timer- or silence-forced sales. |
| R6 | One canonical integer-cent quote; frozen accepted assets/versions/currency/terms; no separate pricing writer. Distinguish selection, intent, provider acceptance, projection, purchase and fulfilment. Unknown delivery retains reconciliation and duplicate protection; no blind resend, automatic extra charge or invented delivery claim. |
| R7 | Evidence-based promotional/business classification; preserve raw history and avoid false exclusions of genuine/ambiguous fans. Separate real, test, suspected-test and promo cohorts. |
| R8 | End-to-end substantive latency from earliest unanswered eligible input, including pacing/queue/model work; oldest-need-first scheduling, cancellation, recovery, bounded concurrency, 429/401/outage behavior, 100 fans/one creator, multiple creators, and 50-creator simulation. Keep targets failed where measured failures remain. |
| R9 | Final combined review; exact compatible images, migration/configuration/recovery packet; two exact approved delivery/unlock checks; staffed bounded pilot; measured commercial outcomes before broader claims. Separate explicit permissions remain mandatory. |

R5 controls already exist through the scoped configuration API: commercial initiative `lower/standard/active`, first window `[2,6]` confirmed raw conversational bubbles, and independent experimental repeat window `[3,6]` completed grouped replies. Windows are reassessment inputs, never send permission. A confirmed paid card closes an attempt; subsequent eligible fan participation, not payment/timers, starts a repeat. Invitations/ordinary topic changes/restarts cannot reset counters. Read the consolidated brief for the full lifecycle and upper-boundary rules; do not replace it with a simpler new seller.

## Work already implemented before this resumed chat

The accepted original implementation had complete-answer/guard, facts/attachment/case, inventory/fairness, canonical quote, durable invitation/commercial progress and intent-filter work. Earlier qualification corrected test discovery that had silently missed pytest bodies, manual-vault quote adaptation, self-blocking durable holds, held-fan preparation starvation, scoped worker lock starvation, budget settlement locking and historical-schema fixtures. The checkpoint documents each repair and original failure.

The accepted concurrent path already introduced/fixed bounded per-fan preparation and send handling, durable fan ownership/leases, off-loop PostgreSQL work, isolated mutable per-fan quote/content state, releasing preparation slots while pacing waits, queued SendGate key reservation/cancellation cleanup, and monotonic shared token-bucket refill. It retained native rate/cooldown/financial authorities. Do not replace it with unbounded workers, another queue or another seller.

The original accepted measurements were synthetic model/provider HTTP: 100 complete replies in 71.905s at the declared higher test allowance, source-paced 100 in 131.368s using the then-25–90 range, and 50 creators x 2 fans in 114.151s with the declared corrected pool. Those are historical results, not the latest candidate or live capacity. Receipt-loss recovery retained a local stopped-turn limitation; do not silently call all claims complete. Retain the prior fault/crash evidence and its limitations.

## Repairs completed in this continuation

1. Recovered the old cloud checkout and laptop repair4 snapshot, compared source hashes and integrated still-needed legacy configuration and historical fixtures. Preserved concurrency and newer branch work.
2. Restored strict old-dashboard response compatibility with explicit negotiation. Commercial configuration fields require `commercial=1`, pacing maximum `replyTiming=1`, vault continuation `progress=1`, folder covers `covers=1`. This changes wire shape, not send permission.
3. Reused one schema-validated control Store/pool per account for fan safety/control operations. Current ownership, hold, price and purchase rows and final wire authority still read fresh state. Children share the validated owner store; changed targets refuse; the owner closes the pool at shutdown.
4. Added `nativeReplyDelayMaxSeconds` through the existing scoped configuration mechanism, stored as `native_reply_delay_max_seconds`; accepted maxima 90/180, existing default 90. Scheduling still anchors to original input. Populated/idempotent pacing upgrade widens only recognised 90-second constraints, preserves every schedule and refuses unknown definitions.
5. Repaired historical test fixtures to use actual historical columns, rather than today's ORM. Retained stronger identity/authentication assertions and durable cases. Unsolicited false identity claims still have a post-generation refusal regression.
6. Fixed test/demo asset copying for the paired dashboard's shared public directory. The disclosed browser assertion patch expects the fixture's truthful missing-provider-connection warning. Product frontend behavior was not redesigned.
7. The broad sweep exposed a test wrapper that failed on the new `io_offload` keyword. It now forwards that argument to the real quiescence function. Every barrier/takeover/reservation/one-POST/one-attempt assertion remains. The focused script and final full run pass it. No production coordination authority was weakened.

No other features or speculative performance changes were added. Preparation 12/16 was slower in earlier diagnostics; retain 8 for the declared test profile rather than multiplying workers.

## Final test and measurement evidence

**Final application/test source: 1b4bbf6.** The complete service sweep finished all **285 modules: 282 pass, 3 fail, zero skipped/empty modules**, with all **958 captured source hashes unchanged**. The three failed Linux modules are:

* `test_twin_bridge_contract.py`: matching real Twin sibling/channel adapter and tsx toolchain missing; not executed.
* `test_twin_bridge_roundtrip.py`: same missing real sibling/toolchain; not executed.
* `test_twin_bridge_runner.py`: Linux has no `cmd`. The exact same test and command-runner bytes separately **passed on native Windows**, exit 0. Keep the Linux failure in the original report; its platform-specific requirement is cleared by `windows-runner.json`.

All **17 modules that failed the earlier frozen PostgreSQL sweep now pass** in the final Linux run. The run retains **43 commercial-state tests**, **26 full-path scenarios**, **800 native PostgreSQL service checks**, **776 local native-service checks**, historical migration/financial/media/hold/recovery/isolation checks, authenticated dashboard HTTP/browser coverage and **4 release rehearsals**. The supplemental managed-proxy suite passed **36 tests**, including races/account isolation. Do not add these overlapping counts into one invented test total.

The interrupted earlier diagnostic completed 267 modules: 263 pass/4 fail, including the wrapper subsequently fixed. Its failed results remain retained. A separate-session proxy setup failure is also retained; the successful rerun owned its PostgreSQL in the same execution session. Driver exit 0 means measurement/runner collection completed, not that every gate passed. Use per-module results and explicit timing denominators.

| Latest frozen workload, synthetic model/provider HTTP | Result |
|---|---|
| 100 fans, one creator, rate 1/send 4/preparation 8, no artificial pacing | 100/100 full replies, 100.099s; first-response p95 95.0318s |
| 100 fans, synthetic rate 10/send 8/preparation 8 | 100/100 full replies, 28.511s; only 65/100 full replies within 20s; **20s target FAIL** |
| 3 creators x 20 fans, concurrent | 60/60 full replies, 24.826s |
| 50 creators x 2 fans, shared simulated pool 64 | 100/100 full replies, 61.632s; first-response p95 55.6333s |
| Full 25–180 pacing, heavier 100-fan multi-bubble/media workload | **85/100** substantive first replies within 180s; 92 by 195.019s, 8 unfinished; p95 of confirmed first replies 187.2223s; **95%-within-180 gate FAIL** |

No wrong recipients, duplicate receipts/sends or after-takeover sends were detected by the final workload assertions. All final owned PostgreSQL servers shut down cleanly. This is not live model/provider, persistent-storage durability, actual WebSocket ingress, immutable production-image, 50 separate production-container or real native allowance proof.

Retained same-host before/after code comparisons used identical allowance within each pair: rate 1 was 106.776s -> 99.750s; separately synthetic rate 10 was 99.873s -> 20.886s. The latter faster diagnostic is **not** substituted for the exact final 28.511s run. Schema validations fell 101 -> 1; safety-scan p95 6.2492s -> 0.0170s; CPU 94.1s -> 17.164s in that comparison. Authority checks remain fresh. The heavy paced run's application/benchmark bytes still match the final candidate; only the unrelated ownership-test wrapper changed later.

## Actual-model gate: not completed

Actual model calls **0**, actual evaluation spend **US$0**. The cloud and laptop processes lacked the named test credential. Do not substitute canned text, regex-only scores or a different model/provider and call this qualified.

The private source preparation retains the original 308-page export, 122 chats and 5,127 messages. The corrected full-context draft has 200 distinct cases, including previously omitted critical incidents and nearby legitimate turns, with no future replies included. Each route has 120 case-route evaluations (primary plus crossover), planned for three generations and bounded repair. The combined planning estimate is US$1.74, not a current billing guarantee. Retain the total **US$2 cap**, one shared durable journal across both routes/resumes, conservative pre-request reservation and authoritative cost settlement. Never clear pending/ambiguous cost or start another journal to evade the cap.

Missing: securely configured evaluator credential/permitted OpenRouter route; original shared journal state; reviewed creator/configuration and operational facts for cases (actual catalogue/assets/quotes/currency/purchases/delivery/holds); reviewed expected outcomes/rubric. Transcript statements are not catalogue or biography authority. Private case IDs/transcripts remain outside GitHub. Reconcile facts before scoring; mark unknowns as unknown.

Score repeated actual generations for completeness, relevance, truthful facts/content/prices, direct requests, clear invitation acceptance, bounded commercial progress, refusal/budget/holds, repetition and excessive suppression. Keep test/suspected-test/promo/real cohorts separate. Do not shrink away required cases to make a budget or pass rate look better.

## Current live baseline and preservation blocker

Read-only snapshot at `2026-11-02T13:31:40Z` (23:31 Brisbane), SSM `b8ad119e-1948-4d27-ac5c-3d3b1d9d293a`, response 0:

* API: `sha256:87bc296eb275e2c220c3daa2726d34c5e9d8c4faa77455b5bf7fd77e8c00f439`.
* Worker/importer/learner: `sha256:6afd1ad6bfc1030cbb0c7cd72d675753cde487b9de64bf0e333b02d89b4831f2`.
* Those images are labelled `9d6756b20063dc7d481f59acfb1f26c65336c357`. Other work advanced them from the earlier observed `91c260b`; this continuation did not deploy either version.
* Supervisor: created, never started, `sha256:64952d348f482a4b40ccd212a5a55d773cab4d322eeea0f1d3eda2e5c9c867b7`.
* Actual database revision: **`0110_recent_history`**. Eight saved policies remain gap/20 seconds. New-conversation automation is false on all eight. Do not interpret sampled runtime flags as full per-fan send eligibility or permission to change them.
* Six sampled ledger scans were idle with recent completion timestamps; two were still running, one stale/deferred and one without a completion. Two accounts had no live-vault aggregate rows. Counts/descriptions alone do not establish sale-ready content or full purchase coverage.

Five **current** worker files still have no matching blob in the authorised repository:

| Path | Current normalised Git blob |
|---|---|
| `service/native_pilot/budget.py` | `6e4618d1f2cb08abf73a2b7eea7c3ca08ac1a13d` |
| `service/onlyfans_read/earnings.py` | `27cb1d895a1650dae67993ce22b9c162abf6b4f5` |
| `service/onlyfans_runtime/profile_learning/evidence.py` | `b56b9dac61d523b53f39cf93f36b45da0a57b9cf` |
| `service/onlyfans_runtime/profile_learning/worker.py` | `a0ef73b23cb6107c9fc453d36ff1f79c5db3612b` |
| `service/onlyfans_worker.py` | `efe744bb11c737bb33b2a2071401a3fbb2d5e324` |

The profile-worker hash above supersedes the older missing `44f2d24...` blob. The current schema blob `ff79958e62805dd4f9a97cd60141eec1918dffbf` is available. Do not overwrite the newer learner using an older recovered file. Source-hash reads were allowed; production source contents were not exported.

Earlier automatic approval review rejected private source transfers to an insufficiently authorised AWS destination and a broader production-source export. **Do not repeat those transfers.** Smallest safe action: have the owner commit the exact current missing source into the already authorised repository. If a copy is genuinely needed, obtain the exact missing destination/payload authority first; do not bypass the rejection through another route.

## Migration and rollback facts

The tested original candidate has **HEAD 0108**; it must not replace the newer 0110 deployment. The valid graph is:

* `0104_contextual_item_scope` -> `0105_operator_cases` -> `0106_read_lane_fairness` -> `0107_active_selling_quote` -> `0108_commercial_progress`.
* `0104_contextual_item_scope` -> `0105_earnings_totals` -> `0106_parallel_history_reads`.
* Both tips -> `0109_history_release_merge` -> `0110_recent_history`.

Use Alembic ancestry, not numeric prefixes, and never stamp away unknown revisions or bootstrap populated data. A separate compatibility checkout passed populated 0108 -> 0110 twice, preserving 155 original tables, unknown/accepted intents, stopped runtimes and proxy evidence. Its latest retained onboarding/scheduler run passed 26 checks, and its read-only recovery API preserved account isolation/closed commands. It is **not** exact current deployed-source or immutable-image parity.

The designated old `d6811b1` recovery source refuses 0108. `fceb624b814435f36be33d0938c3a5b46494a8e5` passes read-only API recovery on 0108 but **fails on 0110**. The reconciled 0110 source passes that read-only test; a matching immutable image is not qualified. No approved current rollback image has been established. Preserve financial/receipt/ownership data and recover forward through positive evidence; do not roll an incompatible reader onto 0110.

Exact candidate CLI for a separately approved explicit disposable/reconciled target:

```bash
PYTHONPATH=service:. python -m db.schema upgrade --url-env ONLYFANS_MIGRATION_URL
PYTHONPATH=service:. python -m db.schema check --url-env ONLYFANS_MIGRATION_URL
```

Use the source supporting that target's actual graph/HEAD. These commands are **not production approval**. Preserve the existing backup/restore quarantine, 5-second lock timeout, 30-second statement timeout, in-flight unknowns and all intentional pauses. See the runbook for detailed configuration/recovery steps.

## What Claude should do next

1. **Re-read branch head and current preservation evidence.** Start from the published source, not an old local repair folder. Preserve subsequent valid work. Read the current five-file manifest and newer 0110 worker changes before selecting a deployable source.
2. **Resolve the reproducible-source blocker.** Recover the exact missing current worker files through the authorised repository/owner. Compare them with the reviewed original candidate and ongoing onboarding work. Integrate only valid, required changes on the qualification branch, keeping Moxy separate. If source remains unavailable, name the exact file/hash and blocked compatibility/image check; complete other independent work.
3. **Make the release source compatible with the real 0110 system.** Preserve both migration branches, valid newer worker/history/accounting work and controls. Qualify populated upgrades, backup/recovery and an exact immutable rollback/forward-recovery option in isolation. Do not install the original 0108 reader, overwrite overlays or alter production while doing this.
4. **Obtain the real Twin adapter prerequisite.** The existing helper needs the authoritative sibling containing `src/config.ts`, the real `src/channels/{identity,http,store}.ts` and matching channel service/lockfile/tsx dependencies. Earlier searches across 63 branch trees/history did not find the required files. Use a real verified `TWIN_ROOT`; do not invent an adapter. Finish the two cross-language integration modules when available. Windows runner proof is already cleared and need not be repeated if its bytes stay unchanged.
5. **Complete actual-model evaluation** with the same configured route, reviewed original cases and shared US$2 journal. Finish completeness/truth/progress/refusal/over-suppression scoring and fix confirmed defects in the existing authorities. If key/case facts remain unavailable, keep that gate blocked explicitly; synthetic results are not a substitute.
6. **Address the remaining measured timing gate within the existing machinery.** Heavy multi-bubble/media pacing fails 95% within 180s. The 20s diagnostic also fails. Use the retained profiles to target demonstrated context/CPU/preparation/send-budget/fairness work. Preserve fresh authority. Do not raise the simulated provider allowance and call it a code gain or native limit, add unbounded workers, change the clock origin, drop unfinished fans, or keep rerunning unchanged loads looking for a passing sample. Verify actual native allowance read-only before proposing a production increase.
7. **Freeze the reconciled final candidate once real repairs are ready.** Reuse unchanged accepted evidence during diagnosis. Run affected modules for each concrete fix, then the complete applicable final suite with source hashes, the 43 state tests, 26 full paths, 100-fan/multi-creator concurrency, financial/migration/hold/recovery and Telegram/FLIRT isolation coverage. Keep every failed/blocked/skipped gate visible. Avoid repeated broad sweeps before the candidate is ready.
8. **Return one complete R0–R9 release packet**, exact branch/commit, before/after measurements, real-model results/cost, current parity, configuration/migration/recovery instructions, remaining exact resources and the two R9/pilot approvals. Do not claim launch/agency readiness until those requirements are actually proved.

These steps finish the original task. Do not branch into unrelated infrastructure redesign, Moxy implementation, Telegram changes, extra products, sales outreach, new UI work or speculative optimisations.

## Two R9 live checks and pilot permission

The revision-2 brief requires **two named approved test accounts**, but does not supply their account/fan/asset/preview/integer-price/currency tuples. The private plan with those values remains missing. Do not choose Gervonta, another known test fan, replacement assets or a price by assumption.

For each original approved tuple, after separate explicit delivery/unlock approval: capture ownership/config revisions and one intent; verify provider acceptance and local projection; inspect the visible recipient/card/assets/count/type/previews/price/currency; perform only the approved purchase/unlock; confirm purchase history and durable ownership; replay reconciliation to prove no duplicate send/payment request. Verify a real handover is acknowledged by the named staffed owner. An invitation or planned card is not delivery proof.

Only after preceding gates and those checks, prepare/obtain a separately approved single-creator staffed bounded pilot. Maile is a proposal only if current inventory, purchase coverage and staffing qualify. Record allowlist, start/end, message/spend bounds, baseline, metrics and abort ownership. The existing runbook's small engineering observation proposal is not activation or commercial proof. Preserve all other accounts' saved states. No automatic expansion.

Abort on wrong recipient, unauthorised action, duplicate send/charge request, inconsistent assets/price, broken stop/takeover, serious identity/consent failure, lost ownership/receipt/fulfilment evidence or unresolved delivery. Do not delete obligations or replay an uncertain POST. Contextual off alone does not revoke independently enabled request/campaign permission.

## Workspaces, private sources and reproducibility

The GitHub branch/docs are the durable review source. Existing local paths, if still available:

| Purpose | Path |
|---|---|
| Cloud backend checkout with recovered work | `/workspace/scratch/86d4770bae26/backend` |
| Earlier repair checkout | `/workspace/scratch/9a82163d809e/backend` |
| Pinned qualification Python | `/workspace/scratch/9a82163d809e/qualification-venv/bin/python` |
| This continuation's final raw results/orchestration | `/workspace/scratch/6112cde2eedb` |
| Final service results | `/workspace/scratch/6112cde2eedb/final-acceptance/results.json` |
| Final load results | `/workspace/scratch/6112cde2eedb/final-capacity/` |
| Private full-context cases/transcript, excluded from GitHub | `/workspace/scratch/9a82163d809e/private-evaluation-source/` |
| Retained paired dashboard | `/workspace/scratch/86d4770bae26/twin-dashboard` |
| Separate onboarding compatibility source | `/workspace/scratch/86d4770bae26/onboarding-compatibility` |
| Laptop snapshots/audit requests | `C:\Users\conta\codex-work\of-original-qualification-20261102` |

The laptop `repair4-candidate` is an older snapshot, not the final application candidate. Only its unchanged Windows test/runner pair was reused, with exact hashes. Do not substitute the entire old checkout.

Final runs used Python 3.12.14/pinned dependencies and owned loopback PostgreSQL 16.15 on tmpfs with the retained test-only UID shim. This is functional/isolated measurement, not persistent-storage or production-image parity. The runner snapshots under this evidence folder document the actual execution; their machine-specific paths/ports must be adapted to a fresh **disposable** environment, never a live DB. Existing portable entrypoint: `scripts/run-service-tests.py --postgres --module-timeout 900 --report <owned-output>`, with explicit test PostgreSQL URL and paired source/dependencies. Timing-sensitive workloads must run without competing qualification jobs.

The real HTTP/browser checks use the restored paired `ce99d8acdc0a09b6492606c1c8ba417b047d6bed` dashboard plus `paired-dashboard-qualification.patch`. This does not prove the currently deployed onboarding frontend's exact source parity. Keep that distinction.

The evidence archive's repeated source-hash maps are stored losslessly as one base plus updates/removals keyed by the reconstructed manifest's SHA-256. Commands, exit codes, source-unchanged flags and original diagnostic logs remain. Raw customer transcripts, credentials, production environment files and unnecessary personal data were excluded from the publication. Do not push those private sources later.

## State when the current agent stopped

Implementation is frozen at 1b4bbf6. The complete final service sweep, required final load measurements, Windows runner check and bounded read-only baseline inspection are finished. Local owned final PostgreSQL servers stopped cleanly. There is no further implementation/test task running from this continuation. Publication/handoff are the only remaining actions of the stopping agent.

**Release remains unqualified.** Concrete remaining blockers are the five current unpublished worker files/source-image reconciliation with 0110, the real Twin adapter/toolchain, actual-model credentials/reviewed case facts/journal, failed heavy timing/20s targets and native/fleet capacity proof, exact compatible release/recovery images, and the missing two R9 tuples/staffed owner/later explicit live permissions. Finish those requirements without adding unrelated work.
