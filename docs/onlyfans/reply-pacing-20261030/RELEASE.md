# Native reply pacing and format release

## Current state

Deployed on 30 October 2026 with the user's approval. Backend runtime source is `d745017852e33df3c7a01f305f116084df2ef7bd`; both dashboard consumers run `2e4dfea7acbc5fa3fe14b64dc2d3bb981b5da7db`. Later documentation commits do not change those deployed runtime sources.

Pacing (Patch A) was enabled for **Maile only** at **07:22:30 UTC / 17:22:30 Brisbane** through the existing authenticated, audited configuration endpoint. Readback confirmed the requested revision and fingerprint. Formatting (Patch B) was also enabled for Maile at **08:06:08 UTC / 18:06:08 Brisbane**, following the user's explicit instruction to make it live after the incomplete live-verification status was explained. Both features remain off for all other creators. Account activation, provider connections, budgets, delivery caps, throttles and other settings were preserved. Nicky/Nikki remains excluded; no accounts were reconnected or newly activated.

At the latest observation before enabling B, 08:00:30 UTC, no new pacing schedules or validated pacing receipts had appeared. There were 355 structured sender events since enabling A, with no recorded pacing-metric, task, reconcile or service-error events. The user then explicitly requested immediate enablement, overriding the original staged waiting requirement. Both settings were read back through the authenticated API with matching audit revision and fingerprint. This is deployment and configuration proof, **not live reply or interruption proof**. No customer test messages or purchases were made.

## Deployed artifacts

| Consumer | Image |
|---|---|
| API | `sha256:ca09c9079b72e045795ee35ce4ab921ac4b7398b95dd885488179de66f6c687b` |
| Native sender | `sha256:2fa1dd0604814eb0a287e67599d4215a648169c3078c1ac0c871069656c01e8c` |
| Dashboard, both consumers | `sha256:60c4edf7947e3af3cbe33454b2df90db508d58572a7a40bf3d5698ec2a33d1fb` |

The final image check found all four intended consumers running their expected images with zero restarts; API health passed. This release did not modify the separate worker. A concurrent release changed that worker from `sha256:3257dba4b75a0a0bbf43a9a0b99a17e04a0af11f76b4105a770b9667fd17c0df` to `sha256:ce1bcd5eeade5381890e633c3127cd0787b48553f43e330a6925e02ab84545e5`; it was running and healthy. Do not overwrite that separate change during rollback.

## Configuration and behavior

| API field | Stored field | Default |
|---|---|---|
| `nativeReplyPacingEnabled` | `native_reply_pacing_enabled` | false |
| `nativeReplyFormatEnabled` | `native_reply_format_enabled` | false |

Both switches use the existing revision/fingerprint and idempotent audit transaction. Only literal booleans are accepted. Missing or corrupt settings cannot enable a feature. Contextual-offer configuration remains independent.

A selects one triangular target between 25 and 90 seconds from first verified ingestion. Its mode is 35 seconds after a confirmed conversational reply in the same current control generation within five minutes, otherwise 60. New bubbles update the latest snapshot without redrawing the target. The seven-second quiet gate is capped at 20 seconds from the first ingestion. Admission subtracts observed median claim-to-first-receipt pipeline time, capped at 20 seconds; an overdue turn adds no new artificial wait.

Waiting creates no model call, engine context, input claim or dispatch allowance. Target identity includes account, fan, binding, activation, connection revision, conversation revision and pacing-setting epoch. Existing claims, unknown-outcome holds, provider retries, per-account throttles and final transport checks remain authoritative. At most three admissions occur per pending burst.

The reconciled live sender already had bounded concurrent preparation and task-local NativeEngine guards. Those are preserved. With A on, the outer sender queue and inner turn both respect the 20-second debounce cap, and its existing `tolerate_newer` escape is disabled so source freshness wins over timing. With A off, the existing sender behavior is retained. Legacy rhythm remains disabled.

B changes ordinary reply instructions and uses the existing splitter for a complete response, optionally in two parts. Transactional replies and sales captions retain their existing handling. The 4,000 UTF-16-unit provider limit rejects oversized text rather than truncating it. B adds no model call or sender.

Only a first validated receipt emits `reply-pacing-receipt`. Metrics distinguish verified-ingestion and provider-created latency, intake lag, target miss, scheduling queue, generation/shaping, account throttle, preflight and transport. A chosen target is not a delivery guarantee.

## Source reconciliation and deployment checks

The earlier API-only candidate could not be safely applied to five differing live sender files. After explicit user approval, their differences were retrieved and reconciled separately. The completion pass preserves the contextual release already deployed at `0b4e7eaf0afd87846723f134313d5db2eb78727f` and the contextual release branch at `817279ceba72eb11c74eca97863caa82a5e90b5d`.

Separate API and sender patches were checked against exact baseline blob hashes, built on their actual images and verified again in running containers. The backend PR is based on the contextual release branch. The dashboard combines the live contextual UI with the two reply controls.

Deploy order was compatible dashboards, then API/sender with both flags off, then the single A canary. The older dashboard's exact-key parser does not accept the new API fields.

Promotion preserved environment, command, entrypoint, user, working directory and effective mounts. The first promotion hit a bind-array ordering comparison and automatically restored the current contextual API/sender baseline. Effective mounts were identical. The corrected order-independent check passed during the second promotion.

The additive migration creates only `fastt_runtime.native_reply_pacing` and its partial pending-generation index. Public Alembic head remains `0101_contextual_offers`. Runtime startup does not silently migrate. Downgrade deliberately retains timing and dispatch evidence.

## Validation

| Check | Result |
|---|---|
| Exact reconciled API image | 114 passed |
| Exact reconciled sender image | 114 passed |
| Real PostgreSQL manual operator and free/paid vault compositor | 35 passed |
| Real PostgreSQL populated kill-switch/rollback retention | 1 passed |
| Local combined pacing, format, recovery and contextual suites | 84 passed |
| Dashboard focused contract and editor TypeScript checks | Passed |
| Dashboard CI creator/upload and full test/build jobs | Passed |
| Dashboard recovery and normal signup/upload boundary checks | Passed |

Earlier ledger, runner, service, engine and post-sale regressions passed on the earlier baseline. They are not represented as exact tests of the reconciled live Engine v2 sender.

The deterministic load test used the actual sender Service and PostgreSQL with a fixed 44-message trace forming 22 two-bubble bursts, two accounts, 12 fans per account, a 20-second throttle, three-second synthetic model, 0.6-second synthetic provider and ten-times scaled clock.

| Run | Replies / model calls | Account 1 p50 / p95 | Account 2 p50 / p95 |
|---|---|---|---|
| Baseline | 22 / 22 | 16.48 / 17.37 s | 16.37 / 16.92 s |
| Candidate, A off | 22 / 22 | 15.79 / 17.53 s | 15.46 / 16.83 s |
| Candidate, A on | 22 / 22 | 65.48 / 87.15 s | 63.32 / 105.52 s |

All runs respected throttles and ended without backlog, errors, crashes or starvation. The A-on run emitted 22 validated synthetic receipt metrics. The tail above 90 seconds demonstrates queue and account-delay limits. This short mocked-socket/model comparison is not a production capacity guarantee. Interruption fences were covered by integration tests, not by this fixed load trace.

## Remaining verification limits

The [dashboard deployment run](https://github.com/twinagency/twin-dashboard/actions/runs/41015046700) deployed and verified source, but its final all-creator catalogue-preview audit stopped after 20 of 1,313 videos with a generic failure. Its exception cause is unproven; this is not recorded as a confirmed timeout. The following public-homepage workflow step was skipped.

The same byte-identical checker on the retained previous dashboard image completed 1,313 checks: 1,309 passed and four failed for one creator. This establishes a pre-existing catalogue verification problem; it does not establish the cause of the candidate's incomplete run. Media routes and the checker were not changed by this feature. The catalogue audit is not marked green.

Actual pacing receipt timing, grouping under live fan traffic, stale-draft interruption, takeover, reconnect/restart and purchase interactions have not all been observed live. No customer test traffic is fabricated to claim those results. Formatting is now enabled for Maile by explicit user instruction while these live-verification limits remain recorded.

## Evidence and rollback

Server-local evidence is under `/var/lib/twin-release/reply-pacing-20261030-208785/`. Configuration, runtime snapshots and detailed observations are private mode-600 files; credentials and per-fan content are not committed. Only aggregate or pass/fail evidence was returned for production checks.

| Action | SSM command |
|---|---|
| Successful backend promotion | `873a74f9-f1b2-4639-8173-faac7968c4a3` |
| OF dashboard promotion | `00b20998-cc63-4eff-ae3d-eda0bc53cc9f` |
| A-only canary enablement | `470660dc-4bb7-4140-a7dd-86175d1d28de` |
| Formatting enablement after explicit user instruction | `4738ae52-da19-4d27-8abc-e4639b7a17a0` |
| Baseline catalogue comparison | `a5b4b770-c8e1-4021-aecf-0292de998a64` |

First rollback is the existing audited setting update to turn A off for the canary. It invalidates waiting schedules and rejects captured stale configuration authority; it cannot retract a call already handed to the provider. Formatting can be disabled independently through the same audited endpoint; this restores baseline style on subsequent turns and invalidates captured formatting authority on unsent work.

If application rollback is needed, refresh the current compose/runtime state under the release lock, then restore only the affected API/sender image references from `promotion-2/backend-compose.before.json`. Do not copy an old whole compose file over concurrent releases. Exact compatible predecessors are API `sha256:cf7dd2382962def7ca18f8b26a39713e61cab5bf5bb637093ce8bf95068527ff` and sender `sha256:42a374a0e29fa08419c2ec4987299d187215a28066528a48d1ac94c79a97e84a`. Retain the additive table and public schema head.

Retained non-running image anchors protect rollback images from pruning. The OF dashboard predecessor container is `next-app-of-reply-rollback-208785`. Retain the compatible additive dashboard parser unless the backend fields have also been reverted.
