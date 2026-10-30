# Native reply pacing and format candidate

## Status

Implemented as separate Patch A and Patch B commits, with independent creator opt-ins. This is a review candidate, not an approved production replacement. No deployment, creator enablement, customer test message, purchase, reconnection or cap change occurred. Nicky/Nikki is excluded. Contextual offers PR #56 remains a separate candidate.

The branch is based on attribution PR #55 at `6a8c99ec2cb096422f4054205120adaa4a6f1294`, not the historical default backend branch. Local source alignment checks matched the six relevant API files against that commit. Production sender source differs in five of those files, so rollout is blocked pending reconciliation.

## Configuration

| API field | Stored field | Default |
|---|---|---|
| `nativeReplyPacingEnabled` | `native_reply_pacing_enabled` | false |
| `nativeReplyFormatEnabled` | `native_reply_format_enabled` | false |

The existing authenticated, audited field-update endpoint owns both switches. Only literal booleans are accepted. Revision, fingerprint and idempotency checks remain in force. Missing flags are off; corrupt values log a diagnostic and cannot enable either feature. Unrelated configuration, including contextual-offer mode, is retained. No creator's stored settings were changed in this implementation session.

The companion dashboard adds two checkboxes to the existing editor. It accepts older API responses with either field absent and disables unsupported controls. The old dashboard's exact-key response parser cannot accept the new fields: deploy the compatible dashboard before the new API. Do not claim old strict clients accept additive fields without updating their parser.

## Flow and delays

Before: current native snapshot -> seven-second quiet gate -> existing fan cooldown -> atomic input claim -> one NativeEngine call -> account throttle -> provider/local preflight -> one durable dispatch boundary -> validated receipt.

Patch A: current snapshot -> persist or reuse one burst target -> return to the serial fan loop while waiting -> read and claim the latest snapshot -> unchanged engine/throttle/preflight/delivery path, with additional current configuration and canonical authority checks at claim, attempt and final transport.

The target is first verified ingestion plus a single triangular sample in [25,90] seconds. The mode is 35 after a confirmed native conversational reply in the same conversation within five minutes and the current control generation, otherwise 60. Scheduled jobs do not supply that signal. Account, fan, binding, activation, connection revision, conversation revision and pacing-setting epoch identify the generation; worker/pilot identity alone does not.

The existing quiet interval remains seven seconds, capped at first verified ingestion plus 20 seconds. A new bubble or caption updates the snapshot without redrawing or moving the target. Admission aims at the target less the observed median claim-to-first-receipt duration, bounded at 20 seconds of allowance. No observations means zero allowance. Eligibility is the later of that admission time and the capped quiet deadline. Overdue work adds no new artificial delay.

Existing delays still apply: two-second poll cadence, serial queue/model time, fan `automation_paused_until`, durable per-account minimum send interval and provider retry-after, account busy wait, preflight and transport. Native legacy rhythm remains disabled and NativeEngine contexts remain serial. This candidate does not establish the current live throttle value for each creator; those values must be refreshed before rollout.

Waiting all-fan children return without sleeping or claiming an input, dispatch allowance or engine context. A short transaction serializes target creation. No transaction remains open over a timing wait or model call. Stale unsent drafts retain the original target under the existing retry rules. At most three admissions are allowed per pending burst; exhaustion holds further model work for review. Existing unknown-delivery holds and receipts remain authoritative.

Patch B changes only ordinary reply instruction/style and uses the existing splitter to preserve a complete response, optionally split at a deliberate two-paragraph boundary. Transactional turn kinds and live sale captions keep their existing prompt and output handling. The provider's 4,000 UTF-16-unit limit refuses oversized output; it does not truncate it. Internal format markers cannot be enabled by a saved configuration shortcut. There is no extra model call or sender.

## Receipt metrics

`reply-pacing-receipt` is emitted after the first validated receipt for an input. It records actual verified-ingestion-to-receipt and provider-created-to-receipt latency, intake lag, selected target, transport start, receipt time, early/late target miss, and clock-skew indication. Timing components separate pre-scheduler delay, intentional wait, scheduler queue, generation/shaping, account throttle, preflight and transport. Nonzero late components are observations, not an invented causal attribution. A metric failure cannot replay a successful send. Scheduling times are never presented as delivered measurements.

## Additive schema and rollback

With both flags off, explicitly run `PYTHONPATH=service python -m native_pilot.pacing_schema --url-env ONLYFANS_MIGRATION_URL` using the release database environment. Runtime startup does not auto-create this table. A flag-on runtime without the migration refuses admission.

The migration adds only `fastt_runtime.native_reply_pacing` on PostgreSQL, or `native_reply_pacing` in SQLite, plus a partial unique index for a waiting account/fan/generation. It requires the existing native schema. It does not advance the public Alembic head or modify dispatch, offer, purchase or receipt records. All consumers must be checked against the reconciled release before migration.

Disabling pacing uses the existing audited settings transaction to invalidate waiting schedules. Configuration revision checks stop an already-generated unsent draft, and unanswered input remains subject to baseline admission. Disabling formatting invalidates captured configuration authority for unsent work and restores baseline wording on subsequent turns. Human takeover remains independently authoritative. An operation already handed to the provider cannot be retracted by these switches.

Schema downgrade intentionally retains the additive table and evidence; the old application ignores it. Populated migration/kill-switch retention is exercised in SQLite. No production rollback was executed. Before any rollout, capture and test an exact compatible rollback for the reconciled sender/API/dashboard images and then-current schema. Do not restore PR #55 over newer live overlays.

## Production evidence and blocker

Read-only source inventory at `2026-10-30T05:21:38Z`:

| Consumer | Image digest |
|---|---|
| API | `sha256:49cf1882099727ab3a6447f3df082c24343572ac4f800242f3f26362cd54b472` |
| Native sender | `sha256:deff89ed978f4e59236387abab1c4bde85aed0e649e0fe23f1a10b009ae47bf2` |
| Worker | `sha256:eb49d6dac1aafa4167a11625256ebaecfb06a5a4f7681c8e38b3049317a64579` |
| Dashboard, both consumers | `sha256:b3d710711da8566d1eae821d11ee38022f5b6833f68e8d1a9774c23bb0a8793a` |

Git blob hashes of relevant live files:

| File under `service/` | API | Native sender |
|---|---|---|
| `native_pilot/service.py` | `64dad3f866567e4ece0a4d9420e7850f7b887992` | `c53efd7181fe221a58145217c9205afd788308c0` |
| `native_pilot/ledger.py` | `5c8a7e4ee311e7f769cd3b9b44571774653f18c2` | `5f4ab7ecea89d8e85a8aa00381e78af809ba7e63` |
| `native_pilot/runner.py` | `ee0ff739efbb266b486d60482f42fccc63f2becc` | `f4b90f4cf4ec940637cebd0f9cf39d1692916ada` |
| `native_pilot/engine.py` | `3e3c5806b712685d759fa3cc47b7ecfa97a1ae57` | `6639dfc0fda1306d0b65973363645d57fb897d3c` |
| `automations/ai_chatter.py` | `6e082f84eb5b8410d8a78599d8081ea911150495` | `485ea399f01a9e70859475611cd44911f2f3aad0` |
| `onlyfans_runtime/configuration.py` | `4f6e357c82f5c68a550701ae08f0587d27552b3f` | same |

A same-host scratch `git apply --check` failed on the five differing sender files. The production containers were not modified. The API-based isolated candidate patch applied cleanly. Automatic approval review rejected exporting full production source diffs through SSM as sensitive source disclosure; no rejected export was retried. Hash inventory and same-host application diagnostics were the permitted alternatives. This is an unresolved release-source mismatch, not a successful production integration test.

The last available prior schema evidence was `0100_manual_free_media`; it was not freshly queried for the production database here. Effective creator revisions/flags, current mounts and all overlays are not a complete deployment manifest. Refresh all of them during source reconciliation.

## Validation record

- Local native ledger, runner and service regression scripts: 300, 262 and 721 checks passed, respectively, with default-off behavior.
- Local native engine: 77 checks passed. Post-sale: 349 checks passed.
- Final local focused pacing/format and recovery pytest suites: 37 tests passed, including lower-bound pipeline allowance and measured timing components.
- Dashboard reply-contract tests passed, including old API compatibility, independent booleans, invalid writes, contextual-field reading and activation-write rejection. Focused editor TypeScript checking passed using the existing dependency fixture. Full Next build and browser interaction were not run.
- First disposable PostgreSQL run: all 15 audited settings tests passed. Thirteen pacing cases failed in setup because synthetic Message rows omitted required `sender_name`/`is_promise` fields. The fixture was corrected. A broader existing regression run exceeded its 240-second budget and is not counted as passing. The disposable network and containers were cleaned up.
- Corrected isolated run: 56 tests passed in 108.05 seconds. Final candidate run: **57 passed** (14 PostgreSQL pacing, 15 PostgreSQL settings, 18 SQLite pacing and 10 format cases), in 113.27 seconds, including confirmed active exchange and the lower-bound measured pipeline allowance. The isolated containers/database and internal network were removed successfully; production was not mutated.

Not verified: identical-load baseline/candidate throughput and p50/p95 delivered latency; oldest unanswered age and model calls per answered turn under sustained load; combined runtime with contextual PR #56; complete manual-media PostgreSQL regression; all production schema consumers; deployment rollback; dashboard browser behavior; actual customer/provider timing, purchases or takeover observations. Synthetic zero failures do not prove zero live failures.

These material dependencies and the failed live-source application block rollout. No canary was selected. After resolving them, deploy with both flags off, verify baseline, refresh a currently eligible already-enabled creator, evaluate A alone on naturally occurring traffic, then evaluate B separately. Insufficient live traffic or missing interruption cases remains incomplete verification.
