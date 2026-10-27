# Astra correction release gates

This candidate is not deployed. Production migration, outbound sends, model calls,
allowance resets and automation expansion are not authorized by this repair task.

## Evidence ownership

The 2,182-case aggregate belongs only to backend
d6811b124928f8167e6625ce9db5db81bf227fad and dashboard
871b3e8955bf8af357f6f66d56c4446bf98fee8e.
Do not apply that result to corrected commits.

Focused correction evidence: 32 campaign runner/transport cases; six late-control
and blocked-candidate cases; 36 broker cases; 231 affected regressions.
The populated release-package run passed three cases: encrypted full-scope restore,
compatible rollback reads preserving every row, and installed session SQL.
These are intermediate results. Final pinned six-shard/native/aggregate results
must be recorded independently after all source changes.

## Running sender reconciliation

Read-only refresh: 2026-10-27T05:36:57Z.
Image: sha256:6e83c77f12e5e1e00d1ecab1b870f247065dbaf2d981bf12a3dc7e334f0b2076.
Image revision label: eff4424e5511aa77f1c89f23d62a3e2a55eb8f45.
Database remains 0092_ledger_history.

| Actual overlay | Disposition |
| --- | --- |
| ai_chatter._runtime_unanswered_replies | Preserved, exact AST match |
| ai_chatter._gather | Preserved, exact AST match |
| ai_chatter._thread_moved_on | Preserved, exact AST match |
| NativeEngine.__init__ | Preserved, exact AST match |
| NativeEngine.unanswered_replies | Preserved, exact AST match |
| db.schema ledger-history declaration | Preserved, exact AST match |
| db.schema HEAD and revision rank through 0092 | Intentionally superseded by 0096 and explicit 0093–0096 migrations |

Refresh command: 2103eb7b-486a-4c2b-89f5-6a53a0ccf6b8.
Semantic comparison: 26540686-1244-475b-b786-639e8f2831f0.
The deployed AutomationRun post-purchase attempt receipt remains in _claim_post_buy,
alongside the additive offer attempt fence. Do not replace the actual-file comparison
with image-label comparison alone. Recheck final runtime hashes before release.

## Required release order

1. Build and retain immutable backend, dashboard, sign-in and broker artifacts.
   Backend API, worker and sender entrypoints must reference the same corrected
   backend image. Record image digests, source trees, dependency inventories and
   non-secret config hashes. Name rollback artifacts before approving deployment.
2. Close command/campaign admission. Stop intake, maintenance, sender/supervisor,
   broker and sign-in browser writers. Disable automatic restart during the window.
   Fence or drain existing grants without deleting uncertain attempts. Confirm no
   late browser/enrollment completion can write during the window.
3. Back up the CRM plus onlyfans_proxy in one encrypted snapshot using the corrected
   recovery command with --include-proxy --source-revision 0092_ledger_history. Retain the existing encryption keys through
   the approved secret store. Separately retain dashboard owner/agency session-store
   recovery material. Record actual snapshot IDs, timestamps and isolated restore
   destination. Never restore stale evidence over newer accepted sends.
4. Run the statistics preflight in one transaction with lock_timeout='5s' and
   statement_timeout='30s': ANALYZE public.messages, public.of_content_sync_source,
   public.of_runtime, public.of_content_sync_account, public.of_offer_import_job.
   Failure stops the release.
5. Run explicit db.schema upgrade/check through
   0093_ledger_markers, 0094_purchase_reply, 0095_content_fold_pending and
   0096_vault_continuation. Verify head, pending-source trigger and index.
   Do not increase migration deadlines or stamp past failure.
6. While broker/sign-in remain stopped, run the existing broker's explicit
   --init-schema upgrade for recovery-upgrade.sql. Never run this on ordinary
   requests. Install the separate management credential only in the management
   execution environment and broker, not in browser/public configuration.
7. Apply dashboard-session-revocation-upgrade.sql to the verified owner-session
   store before switching dashboard code. Preserve existing owner and agency rows.
8. Start only reviewed roles from immutable artifacts with automated sends/campaigns
   disabled. Verify source/config/schema and read/UI behavior. Controlled provider
   verification and explicit release approval remain required.

The 750,000-message migration rehearsal took 12.988 seconds after statistics
refresh. The prior cloud attempt without refresh stalled beyond 22 minutes.
Neither is a production duration guarantee.

## Compatible rollback and restore

Rollback source pair: d6811b124928f8167e6625ce9db5db81bf227fad /
871b3e8955bf8af357f6f66d56c4446bf98fee8e. Both understand schema 0096.
The populated rehearsal starts the old API with read-purpose authentication only,
all mutation capabilities absent and every background writer stopped.
It verifies scoped sales/operations reads and unchanged public/proxy tables,
including accepted and unknown attempts, pending source markers, a frozen marker,
vault progress past 4,800 items, used reservations and reclaim audit.

Read projections take existing authority row locks. A PostgreSQL transaction-wide
read-only setting is not claimed as supported for this API. Enforce closed mutation
admission and stopped background writers; do not grant any sender a rollback restart.

Old production 0092 binaries are not a rollback for 0096. Never downgrade away
durable receipts, grants or pending work. Broker and sign-in stay stopped in the
restricted rollback until compatible versions are approved. Keep the corrected
recovery utility: old utilities intentionally refuse the new public+onlyfans_proxy
archive scope.

A restored database stays quarantined independently of Alembic version.
The broker also refuses the restore fence. Restoring is not permission to resend.

## Legacy session hard cutover

Leave DASHBOARD_ALLOW_LEGACY_ADMIN_SESSIONS unset in production. Legacy HMAC master
cookies must reauthenticate. New master logins use revocable owner_ sessions.
Notify operators of this re-login requirement before release.

The real signer produced a valid legacy cookie in the isolated rehearsal; the
default production policy rejected it there. Server-backed login/revocation and actual logout-action checks
deny copied-token replay, preserve another session and retain the cookie with an
actionable retry if revocation is unavailable. SQL rehearsal verifies existing
owner/agency scopes and repeatable additive upgrade.

## Completed preparation and remaining gates

- Corrected runtime/source pair: backend
  0c4d79aa9f736102aa26bb41e4f7561ab1fff248 and dashboard
  444fd1303858e6d87204efe5811278657571f8f6. Final six-shard/native/isolated-script
  and aggregate status is recorded in astra-corrected-evidence-20261027/README.md.
- Immutable forward/rollback images are built: run 40473190809. Exact image IDs,
  archive hashes and immutable artifact IDs/digests are retained in
  astra-immutable-image-manifest.json. Actual rollback-image populated-database
  rehearsal passed in run 40474649744: 2 cases, zero failures/errors/skips.
  Retain the named packages before their 25 January 2027 expiry.
- The final sender refresh at 08:44:31 UTC matched all 19 reconciled source hashes,
  image and config; production database remains 0092. This bounded read-only check
  must be refreshed at the approved release window.
- Actual production backup/session recovery IDs and the quiescence record remain
  approved-window gates; the isolated rehearsals do not substitute for them.
- Exact controlled test preparation is in astra-controlled-provider-verification.md.
  Current verified connection, actor/revisions, recipient control and explicit
  bounded model/outbound authorization remain required before execution.
- Astra final review and explicit release approval remain required. The historical
  billing-blocked job 121741404579 and old 2,182-case evidence remain historical.
  Current hosted run status must be reported from its own evidence.

Serial NativeEngine/global-hook capacity limits remain. Neither 120-second p95 nor
250-creator capacity is proved. AUTH-02/03 Flirt changes remain paired and deferred.
This repair does not enable them or rewrite scaling.


## Pre-migration backup correction, Astra follow-up

The historical 2,260-case evidence remains attached to backend
0c4d79aa9f736102aa26bb41e4f7561ab1fff248 and dashboard
444fd1303858e6d87204efe5811278657571f8f6. This recovery-code change needs new
exact-pair acceptance and a changed backend forward image before release.
The preupgrade workflow records the new commit and isolated proof; pending
results must not be described as passing.

With every writer stopped in the approved window, verify the actual source is
0092_ledger_history. Retain the 32-byte base64 backup key and existing proxy
credential-encryption key through the existing secret owner, never argv/logs.
Set ONLYFANS_RECOVERY_URL privately to the verified CRM database and
ONLYFANS_BACKUP_KEY to the retained encryption key. Use the corrected candidate:

```sh
PYTHONPATH=service python -m onlyfans_runtime.operations backup /secure/backups/twin-before-0093.twinof --include-proxy --source-revision 0092_ledger_history --pg-bin /usr/lib/postgresql/16/bin
```

The command checks the genuine 0092 structure and refuses known later migration
columns or an unsupported revision. Ordinary backup still defaults to 0096.
Authenticated metadata records schema 0092 and public+onlyfans_proxy scope;
pg_dump excludes active Alembic version data and archive bytes are encrypted
before being saved. Record archive SHA256, backup ID and timestamp privately.

Set ONLYFANS_RESTORE_TEST_URL privately to a new, empty isolated database.
Keep the same backup key and execute:

```sh
PYTHONPATH=service python -m onlyfans_runtime.operations restore /secure/backups/twin-before-0093.twinof --url-env ONLYFANS_RESTORE_TEST_URL --pg-bin /usr/lib/postgresql/16/bin
```

Require successful authentication, all CRM/proxy rows preserved, no active
Alembic revision and the independent onlyfans_restore fence marked quarantined.
Verify both runtime schema checks and broker readiness refuse this destination.
Do not clear the fence, stamp it ready or start sending. Compare source revision
and row evidence before/after backup; backup must not migrate or mutate it.
Retain dashboard session-store recovery material separately.

Only after backup and isolated restoration pass: run the bounded five-table
statistics transaction, then explicit 0092 to 0093 to 0094 to 0095 to 0096
upgrade/check on the verified source. The isolated boundary test constructs
0092 from the frozen 0070 snapshot and historical migration chain, populates
history/unknown-attempt evidence and the pre-recovery broker schema, backs up
and restores first, then rehearses statistics and upgrade on the disposable
source copy. Existing 0096, tamper, crash and nonempty-target refusal cases
remain required. No production execution is implied by this preparation.

