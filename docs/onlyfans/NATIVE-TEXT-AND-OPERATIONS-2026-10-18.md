# Native text preparation and operations — 18 October 2026

Historical 0074 checkpoint. The current extension is
[NATIVE-MODEL-BUDGET-2026-10-18.md](NATIVE-MODEL-BUDGET-2026-10-18.md),
including schema 0075 and nested Operations v2.

This offline checkpoint adds durable native text preparation, authenticated
operational reads, encrypted backups and quarantined restore rehearsals. The
schema head is `0074_native_text_plans`. It extends the one-use transport
checkpoint; it does not complete the full native planner or activate a provider.
No real account, paid model, remote database or deployment was used.

## Native ownership and source basis

The current `Twin-Migration-Source.zip` was located and checked against the
accepted `P2_REPAIR_BASELINE.json`: all 775 source hashes match. The archive
SHA256 is `f3afe90ea7955f9282e09b09b5318c55023fda7ab37ae520fcc3d9b1bf450030`;
the baseline JSON SHA256 is
`f55a187e7ebea25dd528d5d2c4313bdb988e4b7a2040f2816758bdb71acdf538`.
The baseline's historical basis-archive hash identifies an earlier packaging,
not this current ZIP. P2 acceptance is offline Telegram evidence, not acceptance
of this OnlyFans implementation or permission for Telegram/live changes.

The accepted Telegram worker uses creator-scoped SQLite projections and
task-local capabilities. That topology is not imported into OnlyFans. The
separate dirty Telegram/native checkout was not edited. In this repository:

- `NativeTextPlanner` snapshots canonical PostgreSQL history, native fan facts
  and approved creator configuration. It uses the existing `_Cand`, native
  `_turn_kind`, `_build_messages`, `compose_persona` and `finalize_draft` owners.
- Only an explicit `SyntheticNativeModel` is registered. There is no paid model
  fallback, key resolution, legacy automation loop or provider construction.
  The dedicated role also refuses direct legacy `llm_client.chat` calls before
  model/key resolution. This is an internal trusted test seam, not a sandbox
  against arbitrary malicious Python callbacks.
- An explicit text mode in the existing composer carries the repaired source's
  honest identity behavior and approved-facts constraints. It acquires no media,
  sale, sticker, custom, day-log or tip capability. The legacy default remains
  byte-identical across 360 comparisons with the hash-verified Git-base composer.
  No second conversation engine or additional model rewriting layer was added.

## Durable generation and send ordering

`of_native_plan` has one invocation per account/fan/canonical input revision.
Lease generations change on reclaim and therefore are deliberately not the
invocation identity. A generating marker commits before model work; all DB
transactions close before that work starts. An interrupted or timed-out
generation remains unknown and cannot trigger another model call for that input.
Fresh input can supersede it while retaining the old evidence.

Preparation is bounded to the newest 100 canonical non-retracted messages,
64 KiB of context, 20 KiB of output, a 15-second maximum model deadline and a
60-second plan lifetime. A cumulative 1,000-invocation account allowance bounds
the synthetic rehearsal. **This is not the production cost/routing ledger** and
does not import, reopen or reset any exhausted native/Telegram allowance.

Current binding, owner, lease, input, history health, configuration and facts are
checked around generation. Revocation during generation records a stale result
without authorizing work through the revoked binding. Config/fact changes also
invalidate prepared or admitted drafts. Configuration/fan rows are read under
shared locks at final dispatch consumption so ordinary concurrent updates order
against that boundary. Legacy writers still require actual cohort retirement.

Queuing atomically links the plan to the existing `OfTextIntent`. All delivery
then follows the shared prepare/admit/one-use-consume/receipt/materialize path.
Only accepted evidence creates an outbound canonical message. Drafts, unknowns
and queued actions do not fabricate sent bubbles. This path creates no catalog,
offer, transaction, cadence or shadow history rows.

## Operational reads

The existing authenticated read namespace adds
`GET /onlyfans/api/v1/accounts/{account_id}/operations`. The outer contract is
`of-preview.v2`; the scoped nested snapshot is `of-operations.v1`.

The production Twin `/onlyfans/operations` page and matching Next route use the
same positive internal-operator session gate and tenant/account bindings as the
inbox. Agency and view-as sessions are refused. Responses are private/no-store;
failure never becomes a healthy zero or sample data. Strict parsing rejects
unknown fields, invalid counters, another account, future versions and unknown
hold text. Responses contain counters and controlled reasons, never message
bodies, model context, dispatch tokens, credentials or receipt identities.

Counters cover intake, work, draft generation, queued/uncertain delivery,
unprojected receipts and the recorded delivery allowance. Draft counts describe
each conversation's current canonical input. Superseded refusals remain in the
database without holding newer input. Expired ready drafts display as stale;
expired generation displays as unknown without a read rewriting evidence.
These diagnostic reads are not a production monitoring/SLO implementation or
proof of provider connectivity. The page explicitly says the connection is
unverified and live sending is disabled.

## Encrypted backup and safe restore

From the backend root, with explicit variables already exported:

```sh
PYTHONPATH=service python -m onlyfans_runtime.operations backup /private/path/checkpoint.twinof
PYTHONPATH=service python -m onlyfans_runtime.operations restore /private/path/checkpoint.twinof
```

Both commands read `ONLYFANS_RECOVERY_URL` and `ONLYFANS_BACKUP_KEY`. The key is a
base64-encoded 32-byte value stored separately. `--url-env`, `--key-env` and
`--pg-bin` can select explicit environment names and installed PostgreSQL tools.
No key or connection URI is passed in subprocess arguments or printed output.
The CLI does not read `.env`. The two commands require separately selected source
and empty restore-target URLs; never point restore at an occupied database.

Backup streams a consistent custom-format public-schema `pg_dump` through
AES-256-GCM before any archive byte reaches disk. Header and schema identity are
authenticated. Plaintext archives are not written to disk. Encrypted files are
published atomically without replacement, with mode 0600 and file/directory
fsync. The release version row is deliberately omitted from dump data.

Restore snapshots the encrypted input, authenticates every byte before target
mutation, and refuses occupied targets or other user schemas. A separate
`onlyfans_restore` fence commits before single-transaction `pg_restore`. No ready
release row is restored, including during the interval before final sealing;
the final row is `restore_quarantine_0074`. Startup/readiness refuses the fence.
Actual process-death rehearsals cover both pre-restore and post-archive/pre-seal
loss. There is no clear-fence/release command. Restoring old evidence cannot
silently restart a sender, refund uncertainty or overwrite newer authority.

Archive size is bounded to 256 MiB and each PostgreSQL client has a 120-second
maximum deadline. Offsite retention, scheduling, rotation, measured production
RPO/RTO and authoritative provider reconciliation remain release work. This
checkpoint exercised synthetic disposable PostgreSQL only.

## Validation and reproduction

Use a disposable PostgreSQL server in `ONLYFANS_TEST_PG_URL`. Install backend
requirements plus pytest and use the dashboard's locked dependencies. The
acceptance suites are `test_onlyfans_native_planning_pg.py`,
`test_onlyfans_native_upgrade_pg.py`, `test_onlyfans_recovery_pg.py`,
`test_onlyfans_capacity_pg.py`, `test_onlyfans_foundation_pg.py`,
`test_onlyfans_text_delivery_pg.py`, `test_onlyfans_egress_pg.py` and
`test_onlyfans_loopback_transport_pg.py` under `service/tests/`.

The actual authenticated HTTP/browser integration is
`test_onlyfans_http_pg.py`; it uses the existing `preview/onlyfans-integration`
Next harness and synthetic identity HTTP server. The browser has no external
requests. Screenshots are internal review evidence and are not packaged.
Frontend trust checks: `node --import tsx scripts/check-onlyfans-operations.ts`;
also run the existing read/channel checks and root TypeScript check.

The checkpoint's `VALIDATION.md` reports each observed result and its limits.
Queue measurements cover 12 synthetic accounts/192 conversations/four processes
with no duplicate claims, a real local DB disconnect/reconnect and a 1,000-event
intake bound plus 25 backpressured events. These are small shared-sandbox intake
measurements, not 1,000-creator readiness, full reply latency or a production soak.

## Remaining integrated work

Milestone 4 is still open. This is bounded native text preparation, not execution
of the entire native sales/planning loop. Complete native configuration/voice,
summary and corrected-fact persistence/provenance, production model cost/routing
and credential lifecycle before claiming full native text integration. Then add
existing photo/video offers through the same authority, with exact assets,
previews, integer money, ownership, receipts, purchase/refund reconciliation and
partial-action recovery. Do not copy the legacy run/send loop or Telegram runtime.

Also open: authenticated control/resolution commands, real session/connection
verification, Content/Sales/Settings workflows, consumed-budget/unknown import,
cohort writer/schedule retirement, cutover/recovery release, capacity/soak and
separately authorized live acceptance. Preserve prior checkpoints and evidence;
passing this offline slice does not authorize external actions.
