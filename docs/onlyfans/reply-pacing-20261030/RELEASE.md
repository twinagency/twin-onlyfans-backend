# Native reply pacing and format: review candidates, rollout blocked

30 October 2026, Australia/Brisbane. These changes have not been deployed or enabled by this work.

## Status and exact review base

| Stage | Evidence |
| --- | --- |
| Coded | Separate patch A (timing/grouping), patch B (ordinary reply format), and paired existing-editor controls |
| Tested offline | 90 pytest cases plus four subtests; 11 assembled/native regression modules also pass |
| Tested PostgreSQL | 49 unique cases: 47 existing configuration tests plus two new policy/migration/ledger tests |
| Deployed | No |
| Enabled | No creator or fan selected or changed by this work |
| Provider receipts for the feature | None; synthetic boundaries only |
| Actual delivered latency p50/p95 | Not measured; no live canary |

Review base: attribution PR #55, `6a8c99ec2cb096422f4054205120adaa4a6f1294`.
Patch A: PR #57, `dc4c4cd7756ee06dc09a5be05f58710d71bd6d59`.
Patch B is stacked on A, branch `gpt/native-reply-format-20261030`. Dashboard controls are based on main `3a516c8a60401f8d9ed9b30d3e2666f90d7af821`, branch `gpt/native-reply-controls-20261030`.

The relevant original repository blobs were checked against PR #55 before publishing the changed files. The local test checkout also contains previously captured application files needed by the synthetic suites. It is not a claim that the complete repository tree or local test tree equals production. Published patch A blobs match the tested patch A checkout.

## Material live-source mismatch: do not deploy this tree

Fresh read-only AWS SSM inspection of `twin-dashboard-prod`, `i-09a9dd3bd749643a5`, `ap-southeast-2`, found:

| Component | Immutable image ID |
| --- | --- |
| FASTT sender | `sha256:deff89ed978f4e59236387abab1c4bde85aed0e649e0fe23f1a10b009ae47bf2` |
| API | `sha256:49cf1882099727ab3a6447f3df082c24343572ac4f800242f3f26362cd54b472` |
| Worker | `sha256:eb49d6dac1aafa4167a11625256ebaecfb06a5a4f7681c8e38b3049317a64579` |

All three inspected services had zero restarts. Compose `/etc/twin-onlyfans/media-agency-release-compose.json` SHA-256: `b0837e09491a16aaa418ff44f461e8e5bc840fbe8c27bb4d29ed0b4e4d8ac510`.

The live service is **1,718 lines**, with a concurrent task queue, `burst_state`, `account_hold`, `_run_fan`, `_reap`, cancellation/draining, runner leases and a shared send gate. Queue telemetry reported **concurrency 4**. Its current `Service.turn` uses provider-created burst age and sets `tolerate_newer` after `tuning.burst_max_wait_s`. Those facts differ materially from the serial source assumptions in the handoff and this review base.

The live source SHA-256 values were:

| File below `/app/service` | SHA-256 |
| --- | --- |
| `native_pilot/service.py` | `f0019f5d969d40a574767d94995c57d8982061634122b3bdb6886aee5856a854` |
| `native_pilot/runner.py` | `effdb0e152b1ee6c5199cdb46664e75162d338c7435da42762230f5b74aeaace` |
| `native_pilot/ledger.py` | `e5dae32898f5bbf253a836241a3f13756738a618471ed79111646c04b84856dd` |
| `native_pilot/engine.py` | `e06fcdd9d3622fc93898bfeedc59148c69ac3a79a95983339262f3d80850fea8` |
| `onlyfans_runtime/configuration.py` | `f0a18172e91a9a8a958d530027cee7b2633b2ef9084fb86f50821b46f9d96df2` |
| `automations/ai_chatter.py` | `8504a4c9faae3f72f30dd33e7bbf4755a4aa5918cf84554c3dd3a7e5c815d919` |
| `db/onlyfans_pilot_models.py` | `11569368d04c1511a059bb8bb97f3771fa9f7567e7256c51f559cf0233d2843a` |

Do not replace those newer overlays with PR #55 or these branch trees. Reconcile scheduling, generation/freshness, native-engine concurrency, leases, current source, schema consumers, mounts, settings and rollback artifacts before a release. This work has not captured a new complete protected production rollback snapshot or refreshed every live schema/configuration revision. A previous release document's schema version is not substituted for that missing check.

The handoff explicitly requires leaving the feature off when an unsafe assumption or material untested dependency is found. No attempt was made to redesign or remove the live concurrent queue.

## Behavior and independent switches

| API field | Saved key | Default |
| --- | --- | --- |
| `nativeReplyPacingEnabled` | `native_reply_pacing_enabled` | false |
| `nativeReplyFormatEnabled` | `native_reply_format_enabled` | false |

Only literal booleans are accepted. Missing/corrupt stored values are disabled; invalid values emit a diagnostic without exposing saved JSON. Saves use existing actor permissions, revision, fingerprint, idempotency and immutable audit. Field-only saves preserve unrelated JSON, including an existing contextual-offer mode. A stale save is refused. Disabling a reply switch retires waiting/generating/held scheduling authority without deleting claims, receipts or unknown outcomes.

Baseline flow on the review source: snapshot -> seven-second quiet gate -> existing cooldown -> atomic source claim -> FASTT model/format/sale decision -> account pacing -> provider/local preflight -> durable send attempt -> one transport -> validated receipt. Native legacy typing/rhythm remains disabled/no-op as before.

Patch A flow: current canonical controls -> same snapshot -> durable target/admission calculation -> return while waiting -> same atomic claim and FASTT turn -> same send authority plus captured-generation checks -> confirmed-receipt timing evidence. No model, dispatch attempt or open database transaction is held for the intentional admission wait. No second sender, conversation model call, rhythm background job or fan activation is introduced.

The target is drawn once from a triangular distribution, 25–90 seconds, mode 35 after a recent confirmed native conversational reply and otherwise mode 60. Manual/scheduled sends do not establish that signal. The target is keyed to canonical creator/fan, connection/activation/control/configuration generation and the first verified unhandled receipt. New bubbles/caption changes/retries do not redraw it. The seven-second quiet interval is capped at 20 seconds from the first receipt. Recent confirmed admission-to-receipt durations supply a persisted pipeline allowance, capped at 20 seconds; absent observations use zero. Queue/model time count once, and an overdue target adds no new artificial wait. Regeneration uses the existing one-retry bound; continual stale input becomes a visible hold rather than unlimited model spending.

Patch B changes the existing ordinary-chat style slot. It allows a complete brief or detailed response and removes the compulsory bio-question/format instruction only in that scope. The existing splitter gains an optional complete-response path so it does not turn a detailed answer into comma-separated fragments or truncate its tail. Transactional/content/required turn instructions remain on their existing path. Prices, links, validated captions and payloads are not rewritten to create shorter replies. Mandatory filtering, identity handling and existing sales decisions remain authoritative.

The editor has independent “Varied reply timing” and “Flexible reply length” controls. Older API responses remain readable and unsupported controls are disabled. Existing exact parsers across **all** dashboard brands/consumers must be deployed compatibly before the backend returns additive fields. Contextual PR #56 / dashboard PR #335 remain independent; combined integration is not yet accepted or enabled.

## Tests and limits

Local command:

```sh
PYTHONPATH=service:service/tests DATABASE_URL=sqlite+aiosqlite:////tmp/pacing-final-import.db TWIN_FASTT_CONFIG_ISOLATED=1 PYTHONDONTWRITEBYTECODE=1 python -m pytest -q service/tests/test_native_reply_pacing.py service/tests/test_native_reply_format.py service/tests/test_native_pilot_recovery.py service/tests/test_native_pilot_receipt_recovery.py service/tests/test_native_pilot_identity.py service/tests/test_profile_subject_attribution.py --disable-warnings --tb=short
```

Result: **90 passed, 4 subtests passed**. The 36 new cases cover bounded/single sampling, restart, bursts and capped debounce, concurrent target creation, stale-source claims, bounded regeneration, takeover/disconnect/configuration invalidation, unknown outcomes, nonblocking waiting and overdue admission, measured receipt timestamps, migration idempotency, exact default-off prompt equality and ordinary reply preservation. Ten real FASTT synthetic turns exercise brief/detailed/correction/clarification/reference outputs with pacing independently off/on; one ordinary inference per turn and one complete bubble. These are mocked-model/provider examples, not live persona-quality evidence.

Existing script suites pass: ledger (300 checks), runner (262), service (720), engine (77), content, offers (31), vault (56), postsale (349), postsale engine (278), followups (45), resolver seam (21). Earlier separate allowance, manifest and model suites also passed. Repeated runs were used only after the formatting changes or a fixture correction. Recovery/identity pytest modules were executed as pytest tests, not counted from importing a script.

PostgreSQL ran in resource-limited disposable containers on an **internal-only** Docker network with synthetic credentials, no production mounts and no external provider access. `test_onlyfans_configuration_pg.py`: **47 passed**. `test_native_reply_policy_pg.py`: **2 passed**. These exercise real public schema migrations through `0100_manual_free_media`, audited partial saves, stale/idempotent writes, unrelated JSON preservation, independent disable, populated schedule retention, concurrent native target creation, restart and an atomic authority-change refusal before any receipt. The first combined run had 48 passes and one failure because the new test expected the ledger's exception class for an API refusal; correcting only that assertion yielded two new passes on rerun. No application change was needed for that failure. Test containers/network were removed.

Dashboard contract test and focused strict TypeScript editor/dependency check pass. No complete application build or browser/operator walkthrough is claimed.

**Not run / not accepted:** PostgreSQL assembled sender with both feature switches; pacing-on manual free/paid-media regression; full canonical ledger/commerce suite; candidate-vs-unchanged reproducible load with delivered-latency p50/p95, throughput, queue age and model-call components; interruption between actual provider bubbles; combined contextual-offer rollout; complete schema-consumer/rollback rehearsal; full repository suite; live canary and real provider receipts. Existing baseline native tests do not establish these feature-on cases. No live purchase or customer test traffic was initiated.

## Migration, rollback and next release gate

The runtime migration adds only `fastt_runtime.native_reply_schedules` plus its unique pending-burst index. SQLite test fixtures use the same table shape. Public Alembic HEAD is unchanged by this migration. Run `python -m native_pilot.pacing_schema --url-env ONLYFANS_MIGRATION_URL` with the established explicit selector only after the release has reconciled its runtime/schema owners. The normal native schema initializer also knows the additive table.

Rollback retains this table and every input, dispatch and receipt record. Disable each field through the audited field-only update first; restore the exact compatible pre-release executable artifacts captured for that release, not an old repository tree or a historical tag. Never resend ambiguous provider outcomes. A switch cannot retract a POST already handed to the provider. No production rollback was executed because this candidate was never deployed.

Next gate: reconcile with the measured live queue and pass the missing feature-on PostgreSQL/load/interruption checks. Capture exact current images, source, schema, mounts, configuration generations and protected rollback artifacts. Deploy compatible clients and backend with both switches off; prove baseline behavior. Only then select one currently eligible, already AI-enabled creator for naturally occurring patch-A traffic. Exclude Nicky; do not reconnect/unpause accounts, enable new fans or raise limits for testing. Patch B remains off until A has sufficient real receipt and interruption evidence. Insufficient traffic means incomplete verification, not a pass.
