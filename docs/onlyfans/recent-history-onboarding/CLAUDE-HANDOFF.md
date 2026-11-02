# Claude handoff: 30-day creator onboarding and progressive history

Prepared 2 November 2026. This is the continuation document for the onboarding/history work, not a claim that the entire release is complete. The user requested a full handoff and publication of all work to GitHub. No deployment, activation, service stop or application-code change was performed while preparing this handoff.

## 1. Read this first

The reusable backend is deployed as a **Maile-only pilot**, with AI replies disabled. The last verified release is **9d6756b20063dc7d481f59acfb1f26c65336c357**. Initial history coverage is still incomplete. The stored recent incoming messages had all been processed at the latest verified checkpoint, but this does not prove that all messages in the window have been discovered. Maile's authoritative creator onboarding is not approved. The detailed new dashboard card is on GitHub but not deployed because the exact running dashboard source has not been recovered. No fresh-onboarding duration, full-history speedup or 90-minute completion claim is justified.

Start from GitHub and the current live state. Do not restart the initial pilot, reset its cutoff, delete data, repeat the baseline reconstruction or blindly replay deployment/retry scripts. Production importer/learner services were left running. This handoff transfers development ownership; it does not stop production jobs.

The user wants the same reusable behavior for the existing accounts and the next hundred creators. **Every Maile count below describes one account only**, not seven accounts or the fleet. Automatic enrollment for newly provisioned accounts is implemented but remains disabled pending qualification.

## 2. GitHub entry points and source reconciliation

| Item | Exact reference at handoff |
| --- | --- |
| Backend repository | https://github.com/twinagency/twin-onlyfans-backend |
| Onboarding branch | `codex/recent-history-onboarding-20261102` |
| Backend PR, still draft | https://github.com/twinagency/twin-onlyfans-backend/pull/78 |
| Backend head before this documentation commit | `14f73c1eef24121c968e5f6ebd1d793432859211` |
| Deployed application revision | `9d6756b20063dc7d481f59acfb1f26c65336c357` |
| Preserved history/capacity branch | `codex/parallel-history-capacity-20261101`, `e3f033c3bffcd037696a82e79ef50ccc86e939c6` |
| Concurrent/original branch previously integrated | `codex/of-original-completion-20261102`, through `766a7992908e5706d024b90e083d6a260f93a8c5` |
| **New upstream head found during handoff** | **`1b4bbf646adab11e5cdfee4eff25b772cee37dc9`**, not integrated or deployed by this handoff |
| Dashboard repository / PR | https://github.com/twinagency/twin-dashboard/pull/345 |
| Dashboard companion branch | `codex/recent-history-onboarding-20261102` |
| Dashboard application-code head | `5cc43fd2d5fcc0fd4e6be6ff70d3799ff5432632` |
| Dashboard handoff/evidence head | `fc3304205d8d9ac66386aa4ededb09ec41f29586` |
| Dashboard PR base reported during inspection | `main`, `86691f86b06baacf4a1b058f79bf4dd529f07d12` |

The two new original/concurrent commits are `5760c3cfedb9893e06862903bd50d99bc0b58cd5` and `1b4bbf646adab11e5cdfee4eff25b772cee37dc9`. Their messages describe legacy dashboard compatibility repairs, shared per-account control-store pooling, configurable 90/180-second pacing, takeover-race test repair and a release runbook. They explicitly do not claim deployment; heavy multi-bubble latency, 20-second targets and 0108/0110 source/image gates remain open. Read that branch's `docs/onlyfans/final-repair/CONTINUATION-RELEASE-RUNBOOK-20261102.md` and its diff before merging or deploying. Do not treat a stale PR base SHA as the current branch head.

**Critical: the review branch is not a byte-for-byte representation of the entire running container.** Releases use a narrow, committed overlay on pinned immutable production base images. This preserves existing engine and deployment work. The latest overlay explicitly excludes `store.py`: the review branch contains separate operator-case and concurrency changes absent from the running store. A first attempted build caught this mismatch and aborted before production changes. Replacing the full application from the branch would introduce changes that this onboarding release did not deploy or qualify.

The 9d6756b application overlay contains only:

- `service/onlyfans_runtime/history_progress.py`
- `service/tools/history_pilot_measurement.py`
- `service/onlyfans_runtime/parallel_history.py`
- `service/onlyfans_runtime/history_policy.py`
- `service/onlyfans_runtime/connections.py`
- `service/onlyfans_runtime/read_failures.py`
- `service/onlyfans_runtime/launch_history_memory/recovery.py`

The older onboarding implementation is inherited from the pinned prior images. See `deploy/recent-history-20261102/reporting-repair.json`, the earlier overlay packets and the exact build manifest for the complete chain. `read_failures.py` in the prior running image used CRLF; the guard pins its exact prior byte hash `9001291621c5a431dba91ef24fb648def836c796d6f23360c2ec9aa090c852d1`. Do not bypass source/hash guards to make a build pass.

Moxy remains separate and untouched. Preserve valid history-capacity, concurrent-reply and Moxy work. The user explicitly prohibited rebuilding the conversation engine, incorporating the separate Moxy redesign or overwriting newer changes. Report the reconciliation plan before any further deployment.

### Publication and local checkout caveats

The handoff audit compared tracked backend files with GitHub: 1,993 files matched. The only two local/remote differences were separate upstream `docs/onlyfans/final-repair/QUALIFICATION-CHECKPOINT-20261102.md` and `service/native_pilot/tools/load_test.py`; the GitHub versions were preserved. All onboarding source, tests, deployment scripts and existing onboarding evidence matched GitHub before this documentation addition. The backend working tree was clean.

The local backend checkout at `/workspace/scratch/8c1a07a232eb/backend` has a synthetic commit graph (local HEAD `8b66337` before this handoff). **Do not push that graph wholesale or force-push it over GitHub.** Commits were published against actual remote parents through Git data operations with an expected-head guard. Clone/fetch a fresh authoritative GitHub checkout for continuation.

The local dashboard directory is a partial source copy, not a Git checkout. Its application work is already published. Its remaining standalone test harnesses, synthetic rendered HTML and loader were archived under dashboard `docs/recent-history/qualification/` in `fc330420`. They are evidence, not production code or a full Next build. Temporary base64 upload wrappers are transport artifacts, not additional implementation. No private database dumps, environment files, source fingerprints, credentials or raw fan conversations were pushed.

The repository-root `CLAUDE.md` describes an inherited Fastt/SQLite application and even calls this repository public. That overview is stale for this native PostgreSQL release; the backend repository was verified private during handoff. Retain its prohibition on publishing secrets/state, but use this release's manifests, schema and service paths for production operations rather than its generic VPS/bootstrap instructions.

## 3. Original project scope and required outcome

The goal is to reduce creator-onboarding delay and AI expense by processing the preceding 30 days initially, using approved onboarding for creator knowledge and retrieving older conversations progressively for returning fans. Savings must be measured. This is an extension of existing importer, `launch_history_memory`, `profile_learning`, creator configuration and dashboard reporting.

| Requirement | Implemented approach and current qualification limit |
| --- | --- |
| Freeze the initial window | Durable per-binding policy records onboarding time minus 30 days. Restarts, reconnects and flag changes preserve it. It is not rolling retention. |
| Import both directions and attachments | Existing canonical ingestion preserves incoming/outgoing messages and attachment metadata in scope; deduplication, receipts and cursors survive retries. |
| Discover all relevant conversations | Independent thread traversal, full conversation discovery, known-fan sweep and final head catch-up; one old conversation cannot stop others. Failed/inaccessible pages remain incomplete. |
| Reliable cutoff stopping | Provider supports descending IDs, not a verified descending timestamp guarantee. Safe fallback exhaustively traverses ID pages and date-filters persisted initial messages. No date-filter API or snapshot cursor is assumed. Fewer provider requests are not demonstrated. |
| Catch arrivals during import | Complete identity sweeps, durable cursors, overlapping-page deduplication and catch-up passes. Continuous provider offset churn can delay completion; investigate instead of declaring success. |
| Existing accounts and queues | Reuse stored archives/receipts, preserve memories/manual corrections and defer unnecessary older jobs without deleting progress. |
| Same scope for memory | Paid `profile_learning` and deterministic `launch_history_memory` prioritize recent/new input; older sources become eligible only for activated fan history jobs. Bounded batches retain outgoing context. |
| Approved creator information | Existing immutable approval workflow separates public biography, private info, style/examples, boundaries/approvals and pricing/operating notes. Historical chatter cannot silently override approval. |
| Avoid routine creator extraction | Routine outgoing-archive creator extraction is disabled for opted-in accounts. Optional explicitly configured style sample is bounded to 100 messages and still requires approval. |
| Returning fans | One durable creator/fan older-history job, deduplicated triggers, stored-message reuse, prioritization and bounded fair progress. No promise of old history before the first reply. |
| Purchases and operating controls | Chat cutoff does not truncate purchase/entitlement records. Existing authoritative payment/unlock/spend/VIP/fulfilment checks, human takeover, no-contact, isolation and AI enablement remain authoritative. |
| Honest dashboard states | Separate recent import, memory, older history and lifetime status in opt-in API. Failed reads block readiness; cutoff never means lifetime completion; unknown totals have no percentage. Existing UI compatibility is protected. Detailed new card is not yet live. |
| Measured phased release | Per-account pilot, disabled global new-account switch, traceable images and rollback. Maile reuse/new-work accounting exists; fresh end-to-end performance and full-history comparison remain unproven. |

No rate, concurrency allocation or model spending cap was increased for this task. No AI replies were enabled. No existing messages, memories, purchase records or manual corrections were intentionally deleted. A no-facts extraction is valid and receives a processing receipt.

## 4. What memory means in this implementation

This change scopes the existing memory machinery; it does not define an exhaustive new intimate-personal-history collection policy. The user questioned which memories would be collected, including a virginity example. **No dedicated virginity field or new compulsory intimate-history taxonomy was approved or implemented.** Do not manufacture missing profile fields or infer a real fact from fantasy/chatter.

The existing evidence module's fan fields are: `name`, `age`, `date_of_birth`, `location`, `hometown`, `occupation`, `interests`, `preferences`, `recent_topic`, `request`, `family`, `travel`, `event`, `relationship`, `timezone`. These are supported observations from conversation, not required form fields. Existing preferences can map to manually protected preference/fetish fields. Requests, promises, follow-ups and individual conversation continuity also use the existing memory/conversation-state components; this project must preserve them without treating a promise as verified payment or fulfilment.

`profile_learning/evidence.py` checks exact source quotations, supported subject/speaker, permitted fields, source direction and relevant context. Short confirmations need the question/context that gives them meaning. Observations carry provenance; temporal/event/update rules and protected manual fields remain. A source quote alone does not make a fact about the creator into a fact about the fan. Pricing and entitlement decisions still require authoritative operating/catalogue/payment evidence.

Approved creator onboarding separates public material from private internal information. Private fields are excluded from fan-facing prompts. Placeholder text and conflicting PG/PG-13 versus explicit-sexting directions block approval for review; the implementation must not resolve that ambiguity automatically. The conservative validator is not proof that every semantic contradiction can be detected.

## 5. Implementation map

Paths below are relative to the backend repository unless otherwise noted.

| Area | Where to read and what it does |
| --- | --- |
| Policy/enrollment | `service/onlyfans_runtime/history_policy.py`: flags, durable policy/cutoff, source filters, approved onboarding, older-job activation, incomplete-history notice. `connections.py`: small hook in existing `provision_account`. |
| Import worker | `service/onlyfans_runtime/parallel_history.py`: existing bounded pool, account capacity and transient cohort-refresh recovery. Known DB errors back off 1, 2, 4 … 60 seconds; stale cohorts admit no new work until refresh succeeds. A transient seed error defers that account. Unknown failures remain visible. |
| Discovery/recovery | `service/onlyfans_runtime/launch_history_memory/recovery.py`: separate recent and lifetime scans/cursors, two matching identity sweeps, known-fan coverage, final head catch-up, persisted failures and stored-archive reuse. |
| Provider failure classification | `service/onlyfans_runtime/read_failures.py`: message-thread HTTP 404/410 classified as unavailable coverage; preserve cursor, retry after at least an hour, never infer deletion or a successful empty thread. |
| Progress/telemetry | `history_progress.py`, `history_telemetry.py`: scoped counts, pending source receipts, current vs historical errors, actual HTTP attempts, completed/failed/unfinished reads, separate billing categories. |
| Fan extraction | `profile_learning/` and `launch_history_memory/memory.py`: bounded processing, source fingerprints, fact-free receipts, outgoing context, recent-first eligibility and activated older sources, existing manual corrections. |
| Creator approval / use | `creator_onboarding.py`, `launch_personality/workflow.py`, `native_context.py`, `native_planning.py`, plus existing immutable personality approval records. |
| Measurement | `service/tools/history_onboarding_report.py` gives aggregate coverage; `history_pilot_measurement.py` captures private source/receipt/call fingerprints and compares reuse vs new work. |
| Schema | Additive `0110_recent_history`, following merge `0109_history_release_merge`. Current release did not change schema. |
| Release tooling | `deploy/recent-history-20261102/`: initial/compat/cohort overlay packets; reporting-repair packet and build/promotion; qualification; measurement; baseline recovery; guarded memory retry; legacy UI check. |
| Dashboard companion | `components/onlyfans-preview/RecentHistoryStatus.tsx`, `ConnectionSyncStatus.tsx`, `components/onlyfans-launch/memory/BMemoryPanel.tsx`, contracts/parsers under `lib/onlyfans-connections/`, related native API caller and `scripts/check-recent-history.ts`. |

Older-job triggers are committed live inbound events, not every historical import. The durable creator/fan pair key prevents restarts on every new message. References to earlier conversation, commitments and payment context can raise priority. Existing stored older incoming messages are processed before more provider reads. Aging and periodic older turns (including the 30-second scheduling interval in the existing lane) prevent permanent starvation within available capacity. Live/recent work remains higher priority. Zero older jobs were activated in the recorded Maile checkpoint; this path is synthetically tested, not a completed live returning-fan benchmark.

## 6. Exact deployed state and rollback inventory

Last successful independent production readback was after the 2 November release at approximately **23:24 Brisbane / 13:24 UTC**. All four backend processes were running with zero restarts and the expected OCI revision; API health was 200. A read of a saved build-result file through the remote-control laptop during handoff timed out, so this document does not present a new live health observation. Recheck before acting.

| Component/control | Last verified value |
| --- | --- |
| Worker/importer/learner image | `sha256:6afd1ad6bfc1030cbb0c7cd72d675753cde487b9de64bf0e333b02d89b4831f2` |
| API image | `sha256:87bc296eb275e2c220c3daa2726d34c5e9d8c4faa77455b5bf7fd77e8c00f439` |
| OCI application revision | `9d6756b20063dc7d481f59acfb1f26c65336c357` |
| Schema | `0110_recent_history` |
| Pilot account | Maile, `of-323358988` |
| Policy start | `2026-11-02T09:44:23.165525Z` |
| Fixed cutoff | `2026-10-03T09:44:23.165525Z` |
| AI replies | Disabled |
| Explicit pilot allowlist on all four processes | `ONLYFANS_RECENT_HISTORY_ACCOUNTS=["of-323358988"]` |
| Automatic new-account switch | `ONLYFANS_RECENT_HISTORY_NEW_ACCOUNTS` absent/default `0` on all four |
| Learner cohort | Maile-only `ONLYFANS_PROFILE_ACCOUNTS` retained; review this restriction before expansion |
| Maile provider/model limits | 2 requests/sec, 8 inflight; US$6/day model cap, unchanged |
| Latest controlled restart interruption | 33.788651 seconds |
| Supervisor | `twin-onlyfans-fastt-supervisor-1` remains stopped/created |
| Supervisor image | `sha256:64952d348f482a4b40ccd212a5a55d773cab4d322eeea0f1d3eda2e5c9c867b7` |

Host containers: `twin-onlyfans-worker-1`, `twin-onlyfans-api-1`, `twin-onlyfans-history-importer-1`, `twin-onlyfans-profile-learner` (no trailing `-1`), database `twin-onlyfans-database-1`.

Release root **P** = `/var/lib/twin-release/recent-history-20261102-resume`.

- **Current manifest:** `P/repair-9d6756b20063/deployment.json`.
- Exact staged source/scripts: `P/source-9d6756b20063/`.
- Current build and qualification: `P/repair-9d6756b20063/build.json`, `qualification.json`, `qualification.log`.
- Prior compose/container/flag-off rollback records inside that repair directory: `compose.before.private.json`, `containers.before.private.json`, `rollback.flag-off.private.json`.
- Active compose: `/etc/twin-onlyfans/media-agency-release-compose.json`.
- Both release locks: `/var/lock/twin-onlyfans-native-release.lock` and `/etc/twin-onlyfans/paid-media-release.lock`.
- **Historical, not current:** `P/deployment-repair.json` identifies 91c260; `P/deployment.json` identifies the initial 9870 pilot.
- Exact release ZIP SHA-256: `a3bea9ae8ecb0b806bec284b0cbec089ed77ce2216cbfc4b17a5fd4a7c4a1ca2`; private artifact at `s3://twin-worker-transfer-38e051d1/onlyfans/releases/recent-history/9d6756b20063dc7d481f59acfb1f26c65336c357/release.zip`.

The committed build/promotion scripts intentionally pin earlier source/images and are **not generic idempotent deploy commands**. They can refuse after a successful promotion. A future release needs a new reviewed manifest and exact-current-state guards, not removal of guards or manual edits in containers.

Rollback must use schema-compatible binaries/configuration and retain all messages, purchases, memories, receipts, cursors and fixed cutoff. Schema-0106 images are not a safe image-only rollback after 0110. The retained flag-off rollback is the minimal policy rollback; automatic enrollment is already off. For a future expanded rollout, turn off both the explicit allowlist and automatic switch consistently, and stop/coordinate scoped work under the existing release controls. Never enable replies or start the stopped supervisor as part of rollback. Do not execute rollback merely to prepare this handoff.

## 7. Dashboard mismatch and compatibility

| Running dashboard | Last verified identity |
| --- | --- |
| OF container | `next-app-of` |
| OF image | `sha256:22be6bc89bf7308e2c2214e03d63313bfc32f2edb6c831bddcd1a06a28258049` |
| Reported OF Git SHA | `3d0ee2e3e2848f9cd5019399a82fe27a67f435c2` |
| OF Next build ID | `xvlzKe6xnQRSavfQQmgfM` |
| Image created | 1 November 2026, 14:39:35 +10:00 |
| OCI source/revision labels | Absent |
| Main container | `next-app` |
| Main image | `sha256:ab10c2335b9771af12403b64a7db9982e134ac5e421d76b14840fedcb4fca9e4` |
| Main reported source | `2c6ac02f8b76b5cc6d22a690dd5fb047f3cc6844` |

Both dashboards were preserved. The OF source object returned GitHub 404 at the last source investigation; 357 branch heads, release directories, the older checkout, available CodeBuild/S3 records and another release host did not recover its matching source. A reported environment Git SHA is not proof that the image was built from a complete, committed tree. The missing material is the original builder's Git objects or full protected source/build-context archive for this exact image, including any uncommitted build inputs.

The backend can run independently because its new history payload is opt-in with **`history=recent`**. Legacy callers retain their exact response shape. While lifetime history is incomplete, projected legacy `sync.recoveryRequired` stays true and `lastCompleteSyncAt` is null. Stored timestamps/cursors are not erased. The actual compiled label logic from `/app/.next/static/chunks/684-ea17ce441b70e761.js` was executed in two checks to verify it does not label recent-only completion as full lifetime completion.

The detailed four-state card is in PR345 and has parser/component evidence, but **not a qualified complete Next build on the live source**. The smallest safe route is to preserve both running dashboards, recover the original builder source, compare it to the running artifacts and newer branches, then port the existing small card/API changes onto that reconciled source. Do not rebuild from an older branch or treat decompiled chunks as a complete source recovery. If source remains unavailable, backend coverage work can continue with the compatible existing dashboard and aggregate reports; the detailed-card acceptance criterion remains blocked.

## 8. What was fixed already; do not repeat

| Work | Status / evidence |
| --- | --- |
| Initial 30-day policy, scoped queues, approval authority, older-job machinery and schema | Initial Maile release `9870b64889833b16a36397c8ffa45dc7e236cea3`, 19:44 Brisbane; 66 checks. |
| Initial importer DB-lock exit | Other handoff reports existing container restart at about 19:59 under both locks. Later code now handles startup/periodic transient cohort refresh and per-account seed failures with bounded backoff. |
| Empty baseline and reporting repair | `c8216d33ef409be5bc99aed0f654b1ccfe3023b3` plus follow-up recovery/reporting work. Docker stdin corrected with `-i`; durable private snapshot/validation safeguards retained. |
| Overlapping standalone patch | `91a8325b1963b0b134d81d7008bc366f93cf51ef` was compared, not wholesale cherry-picked. Its stdin fix overlapped newer work; remaining useful safeguards were integrated. |
| Reconciled reporting/importer repair | `91c260ba54451ba51525cd3cc2ab0737bedc1592`, deployed about 22:19; 75 checks. Preserved concurrent lineage through 766a799. |
| Baseline reconstruction | Completed from verified private pre-pilot dump in an isolated disposable DB; cleanup completed. Recovery commits `62191c352e4940cf7c2f9ae9c1709da3e37cac54`, `d0d100cf9a4ecb02a62d61579ff51a41a284bdd0`. |
| Historic model errors / pending recent messages | Reporting/retry code `a5102ec28b7dab4a415f35f5663160df3850e0c9`, recovery evidence `5649613bb311eea45eada20d6bba4c537ed7ca6b`; guarded retry completed. |
| Initial new-account release stage | `05b0f26e05131e4ba9e6b77ba63f9676b2f8100b` failed its source guard safely before production changes. The mismatch was reconciled, not bypassed. |
| Scoped latest release | `9d6756b20063dc7d481f59acfb1f26c65336c357`: no store replacement; current error scope, unavailable-thread handling, gated new-account enrollment; 79 checks; deployed. |
| Latest recorded release evidence | `14f73c1eef24121c968e5f6ebd1d793432859211`. |

### Memory request failures were diagnosed and recovered

The earlier report of 14 failed jobs and 30 pending incoming messages is **historical**, not the latest state. Diagnosis found old OpenRouter HTTP 402 credit failures predating this pilot; current pilot calls were succeeding. At the narrowed diagnosis, 13 historical flags remained: nine without pending recent sources and four with pending recent sources.

`retry-recovered-memory.py` is read-only by default. Its guarded apply path required disabled replies, the current policy, successful newer calls, no post-pilot failed call for the fan, no active lease, future retry, exact pending receipt eligibility and at most four selected jobs. It changed only `next_at`, leaving attempts/error evidence until normal worker success. SSM `5db3f863-75b7-4133-8a63-995e523c658e` rescheduled the four eligible jobs. Normal processing then cleared the pending backlog. **Do not rerun this helper blindly or reset all failures.** Nine historic flags remain preserved and separately reported as `errorsOutsidePendingRecentWork`.

### The baseline is reconstructed, not an original successful capture

The original `P/pilot.before.private.json` is empty because initial promotion omitted `docker exec -i`. The original deployment's baseline claim is corrected in current evidence. The pre-pilot dump was 750,543,194 bytes with SHA-256 `8dd2fcd543acbba784c8c8e1b2d4c268cfcf90739489bcfbeff323f207621337`. Its transaction snapshot lies between `2026-11-02T09:42:23.355491Z` and `09:43:45.361569Z`, preceding policy start at `09:44:23.165525Z`.

Successful isolated recovery produced `P/pilot.recovered-before.private.json` and `P/baseline-reconstruction.json`. The backup-to-activation gap is explicit in comparisons. Recovery used an internal network/disposable database, no provider/model calls or production DB writes, and longer query timeout only in the isolated clone. Its temporary DB/network was cleaned up. The measurement script uses a nonempty original snapshot if present, otherwise the recovered snapshot. Do not rerun the recovery as a continuation prerequisite.

## 9. Actual Maile measurements and their limits

Source: [evidence/measurement-9d6756b.json](evidence/measurement-9d6756b.json). Coverage timestamp: **2026-11-02T13:24:11.001808Z**. Comparison and coverage are sequential reads, so concurrent arrivals explain small differences. These results concern an existing archive and reused extraction, not fresh creator onboarding.

| Coverage / processing metric | Recorded result |
| --- | --- |
| Stored messages in fixed window | 91,727 |
| Fans represented in stored window | 16,573 |
| All stored messages for current binding | 98,869 |
| Incoming window messages processed / present | 2,841 / 2,841 |
| Pending incoming window messages / current-window errors | 0 / 0 |
| Historical error flags outside pending recent work | 9, preserved |
| Active budget hold | None |
| Recent discovery | Incomplete; zero completed discovery passes |
| Conversations awaiting recent coverage / ready | 23,897 / 0 |
| Provider errors | 286 protocol, 9 timeout, 1 transient HTTP, 2 conflicting-history |
| Provider HTTP attempts | 8,179, counted at dispatch including identity probes/failures |
| Page read attempts / failed / unfinished | 4,093 / 305 / 1 |
| Time since policy start | 13,187.371718 seconds; import still incomplete |
| Lifetime completion | False; 161 previously complete lifetime conversations |
| Activated older fan jobs | 0 |

The outer legacy history object has its own discovery fields. For initial 30-day readiness, use **`progress.recentHistory.initialHistory`**, not the outer legacy `discoveryComplete`. `memoryProcessing.state` remains incomplete while initial history coverage is incomplete even when all currently stored incoming messages have receipts.

| Reuse vs new work | Recorded comparison |
| --- | --- |
| Pre-pilot window source records | 88,539, all unchanged and reused |
| Baseline source records edited / lost from scope | 0 / 0 |
| Newly observed source keys | 3,181, including live arrivals and backup-gap arrivals |
| Canonical messages actually added by history pages | 1,893 |
| Completed incoming receipts reused | 2,714 |
| New completed incoming receipts | 127, including 63 for reused sources |
| New model calls | 68 |
| Actual provider-reported charge for new calls | **US$0.0106**, 106 units at US$0.0001 |
| Token estimates / active reservations / unresolved reservations in this scope | 0 / 0 / 0 |
| Settlement changes on pre-existing model calls | 0 in this comparison |
| Cumulative new model-call latency | 74.312 seconds |
| New receipt commit span | 13,077.477805 seconds; not total memory-processing duration |
| New provider attempts at earlier comparison read | 8,163 |
| Comparison elapsed from reconstructed baseline | 13,305.883205 seconds; includes backup-to-activation gap |

No total memory completion duration is established until coverage and eligible processing finish. Cumulative model latency is not elapsed onboarding time, and receipt span includes waiting. The stored window has about 80.9% of stored incoming character volume; that is neither a measured 19.1% saving nor a lifetime estimate. Unknown unimported history, provider failures, existing receipts, live arrivals, validation time and restarts prevent a defensible fresh full-history speedup calculation from this run. Do not pay to re-extract reused data just to manufacture a benchmark. Design an explicitly scoped comparison and report its assumptions/cost before any additional paid baseline run.

## 10. Tests and evidence

The exact latest immutable-base qualification passed **79 tests in 353.99 seconds pytest / 357.439556 seconds wall**, with synthetic provider/model behavior and a real isolated PostgreSQL database. Test image: `sha256:791c702de7241f54c41bc6d11950cc5a31bd8c5594615165a070571563c71b77`.

The suite in `deploy/recent-history-20261102/qualify.py` covers:

- `tests/test_recent_history_onboarding_pg.py`
- `tests/test_history_pilot_measurement.py`
- `tests/test_parallel_history_scheduler.py`
- `tests/test_profile_learning_cohort.py`
- `tests/test_onlyfans_connection_states_pg.py`
- `tests/test_onlyfans_connections_pg.py`
- `tests/test_onlyfans_launch_personality_workflow.py::test_structured_approved_onboarding_excludes_private_data_and_beats_archive_claims`
- `tests/test_onlyfans_launch_personality_workflow.py::test_structured_onboarding_conflicts_block_existing_approval_atomically`

Coverage includes cutoff edges, discovery/pagination/catch-up, dedup/restarts, archive/manual-memory preservation, activation deduplication, old extraction eligibility, creator authority/private exclusion, failed coverage, disabled replies and connection controls, transient DB recovery, pending-only memory errors, unavailable-thread retry, 100 synthetic account registrations and simulated scheduling of 100 accounts through a two-worker pool. **Synthetic scale tests do not prove real 100-creator provider/model throughput.** Existing purchase/control regression evidence is preserved; this task did not change the purchase ledger or broaden activation.

Earlier test counts overlap and must not be added together: initial deployed 66, c821 candidate 67, 91c260 deployed 75, local latest 63 and scheduler eight; earlier broad 211 and focused suites are retained in evidence. Memory-reporting suite 23 and focused retry five also overlap. A retry test fixture initially omitted required `was_dry_run`, was corrected and rerun successfully; no production schema change was made for that fixture failure.

Dashboard evidence is 12 parser checks, four Chromium component scenarios and two checks of the actual legacy compiled label. This is not a complete Next.js build, production CSS verification or live frontend deployment. GitHub Actions startup failures were not counted as passes. The upstream concurrent-reply standalone program's 800 checks were not rerun here; passing that script to pytest does not collect them. Separate R9/original-completion qualification remains separate and open.

Evidence entry points:

- [RELEASE-9d6756b.md](RELEASE-9d6756b.md): current backend release.
- [NEW-ACCOUNT-ROLLOUT.md](NEW-ACCOUNT-ROLLOUT.md): switch semantics and capacity limits.
- [evidence/deployment-9d6756b.json](evidence/deployment-9d6756b.json): current deployment identity.
- [evidence/measurement-9d6756b.json](evidence/measurement-9d6756b.json): current recorded aggregates/provenance.
- `RECOVERY-2252-BRISBANE.md`: earlier successful memory recovery checkpoint.
- `RECONCILIATION-20261102.md`, `MAILE-MEASUREMENT.json` and older README sections: historical snapshots, not current live status.
- Latest full qualification log/JSON and image manifests remain in the host repair directory identified above. They were not newly reread during this handoff because remote readback timed out.

## 11. Remaining work, in order

1. **Re-establish authoritative live and source state.** Read current branch heads, the two newer original/concurrent commits, current OCI images/configuration/schema and Maile's cutoff/reply flag. Compare actual image files with the narrow overlay manifests. Preserve both dashboards, stopped supervisor, current Moxy work and existing controls. Report the resulting reconciliation plan before deployment. Do not merge the whole branch into production just because onboarding tests passed.
2. **Finish recent history coverage.** Determine why discovery remains at pass zero and all recent cursors await coverage. Investigate bounded samples of protocol failures/404s, timeouts and conflicting-history records using existing provider identity/auth/route controls. Do not assume “thread not found” proves deletion. Keep inaccessible threads visible and incomplete; if the provider permanently withholds history, explicitly report unavailable coverage rather than falsely satisfying the original success criterion. Investigate cutoff/pagination behavior without assuming IDs guarantee timestamp order or inventing a provider filter. Keep rates/backoff/budgets fixed. Do not reset durable progress.
3. **Monitor scoped memory processing as more data arrives.** The previously blocked recent backlog is recovered. Let normal receipt-aware jobs handle arrivals. Only diagnose/retry newly evidenced eligible failures. Preserve nine historic flags and all manual facts; never blanket-reset the queue or charge again for completed receipts.
4. **Recover and approve Maile's actual onboarding information.** All 25 saved personality revisions inspected were drafts; none contained `approvedOnboarding` or `review.onboarding`. Current revision 25 had `automaticProfile`, `draft`, `review`, `source`. Accessible Drive searches found a Maile full-history folder (`1n-c1Tgt9oueHOs8RXVHrLPiYKvqlfuu-`) but no accessible authoritative onboarding sheet. Generic templates/another creator's information are not Maile approval. Obtain the correct source, separate public/private/style/boundaries/pricing, resolve placeholders/conflicts through the existing review workflow and then approve. Do not approve historical guesses automatically.
5. **Recover matching dashboard source and finish the small UI change.** Request/recover the original builder checkout or source archive for reported SHA 3d0ee2e3/image22be6bc8. Reconcile newer dashboard/Moxy changes; port PR345 and qualify the full build. Backend compatibility allows steps 2–4 to continue while this is blocked. Do not replace the running dashboard from an older source tree.
6. **Produce a defensible measurement and decide rollout.** Capture final recent coverage, memory completion, request counts, elapsed time and provider-reported actual charges separately from reservations. Keep archive/receipt reuse, new history fetches, new extraction and live arrivals distinct. Qualify at least one controlled new-account onboarding before making fresh-time claims; a partial Maile rerun is not that test. Compare full-history work only on a disclosed, comparable basis and preserve existing spending caps.
7. **Expand only through reviewed flags/configuration after pilot gates pass.** `ONLYFANS_RECENT_HISTORY_NEW_ACCOUNTS=1` enrolls newly provisioned accounts atomically at their activation time; it does not silently adopt existing unflagged accounts. Existing account flags remain available. Enable consistently in API/worker/importer/learner and review/remove the Maile-only learner cohort as appropriate. Automatic cohorts use the existing bounded pool/default account limits unless already configured otherwise. Keep account isolation, manual enrollment decisions, budgets and disabled-reply settings. Test operational capacity progressively; the synthetic hundred-account tests are not a load guarantee.

If exact dashboard source remains unrecoverable, explicitly identify the missing original Git objects/build context and keep the compatible backend pilot plus old dashboard intact. That source-control blocker is not evidence that a 30-day processing scope failed. Conversely, extraction scoping alone does not establish reduced provider download time while exhaustive traversal is required.

## 12. Operational access and safe continuation

The successful route used the authorized Windows control laptop via Remote Desktop Commander, then AWS SSM. Device ID `743b5427-502c-4ac7-9064-47b724f961f7` (`LAPTOP-PKFJ7M3I`); AWS profile `jesse-gpt`, region `ap-southeast-2`; instance `i-09a9dd3bd749643a5` (`twin-dashboard-prod`). These are identifiers, not credentials. Use the existing authorized credential mechanism. The accessible Supabase app project is not this operational OnlyFans PostgreSQL database.

Remote command readbacks are intermittent. A tool returning a running `call_id` must be polled with `get_tool_result`; do not repeat a side-effecting call because output is late. For SSM, save the returned command ID and invocation JSON to laptop temporary files, then inspect that command before sending another. PowerShell `Tee-Object` was used to retain IDs/readbacks. Do not dump compose environments, tokens, private source snapshots, raw fan messages or signed artifact URLs into chat/GitHub.

Successful command IDs useful for recovery:

| Operation | SSM command ID |
| --- | --- |
| Latest build / 79-test qualification | `02cbe2b4-8bd5-4283-90cd-32b93bb3baef` |
| Latest promotion / measurement | `2cc84454-dc52-4a89-bf28-c1df5fb6b536` |
| Independent latest image/health check | `8031a749-2837-4e44-bee6-4c6303910f90` |
| Guarded completed memory retry | `5db3f863-75b7-4133-8a63-995e523c658e` |
| Successful isolated baseline reconstruction | `9f76aa26-3302-48b0-afe3-bfced8a56616` |
| Prior 91c260 promotion/measurement | `ebf8874a-de09-4cbc-b09a-e9d3513b5c35` |

Saved laptop result files under `C:/Users/conta/AppData/Local/Temp/` include `twin-rollout-final-build-result.json`, `twin-rollout-final-promote-result.json`, `twin-rollout-final-health-result.json`, `twin-scope-diagnosis-result.json`, `twin-approved-inventory-result.json`, `twin-postretry-coverage-result.json`, `twin-recovered-memory-retry-result.json`. These may outlive an unreliable command session; host manifests are the canonical deployment records.

Safe read-only commands on the production host, after confirming the correct target:

```bash
docker exec twin-onlyfans-worker-1 python -m tools.history_onboarding_report --account of-323358988
```

For a new reuse-vs-new checkpoint, review and run the **current** committed `measure.py` from `P/source-9d6756b20063/` at its staged relative location. It reads the production DB, makes no provider/model calls and writes exclusive private/public timestamped snapshots on the host. It asserts replies are disabled. Never publish its private fingerprints; only publish the aggregate public report. Keep `docker exec -i` when passing snapshot JSON through stdin.

For isolated tests, inspect `qualify.py` and its `build.json` input first. It starts disposable Docker resources, not a production DB test run. In the old local workspace, `/workspace/scratch/8c1a07a232eb/run_tests.py` uses a shared isolated PG namespace/port 55441 and `/workspace/scratch/9a82163d809e/qualification-venv/bin/python`; do not run two copies concurrently. A fresh Claude environment should provision its own isolated test environment rather than assume these scratch paths persist.

There was no new deploy/build/promotion left pending by this handoff. One read-only Desktop Commander request for the saved build-result file returned repeated lookup timeouts; it had no production mutation. Do not confuse it with a pending deployment. Existing live importer/learner work continues.

## 13. Copyable instruction for Claude

> Continue the 30-day onboarding/history release in twinagency/twin-onlyfans-backend. Read this entire handoff and its linked release/measurement evidence first. Fetch the actual latest onboarding and original/concurrent heads; reconcile the new original-branch commits and exact running image overlays before proposing a deployment. Preserve Moxy, concurrent replies, all existing archives/memories/purchases, the frozen Maile cutoff, current provider/model limits and disabled replies. The backend is already deployed at 9d6756b for Maile only. Recent stored memory backlog was zero at the last checkpoint, but recent coverage, creator approval, matching dashboard source and a fresh end-to-end benchmark remain unfinished. Do not redo the successful baseline reconstruction or blanket memory retry. Work narrowly on the listed blockers, keep every source/configuration change in GitHub, report the reconciliation plan before deployment and distinguish verified facts from unmeasured expectations. The user wants completion with minimal necessary changes, not another redesign.
