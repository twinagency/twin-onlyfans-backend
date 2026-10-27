# Twin release state: 28 October 2026

**Decision: read services deployed; sender activation held. This is not a completed public launch.**

The API, background reader, proxy broker, creator sign-in and dashboard are running. Public login returns HTTP 200. The existing FASTT sender remains stopped and the API command credentials remain fenced. No account activation, allowance reset, manual fan message or uncertain-attempt resend was performed.

## Exact versions

| Role | Source | Immutable Docker image ID |
| --- | --- | --- |
| API, reader, broker; prepared sender | `293b23dbdf8dd5e97f27d6c0bef8ef4710415c06` | `sha256:b4c3ec13342111c1eadc75ad224ab314b659ff60494d78513b37dbdeb67e33ec` |
| Dashboard | `4599999761dbf2ff2b517359642d10262324867a` | `sha256:fccf205a34fe002c6344062305128f6b302a2e1e0c5d92caf7beac30d2a79c95` |
| Creator sign-in | Committed server file matches backend 293b23d | `sha256:b63d926bf03c7b6abe5a52c9bfa77b4d6821d7543d16ec1ab64155c1dc925b6e` |
| Restricted backend rollback | `a550199c42a9873c59c5bd74f9c2d7bd1793d392` | `sha256:2b84c20fd69de3df26e7ce52079efde0fac8e0e5be2bf49d39fd10b63a18e46f` |
| Restricted dashboard rollback | `871b3e8955bf8af357f6f66d56c4446bf98fee8e` | `sha256:4c0b428548486845372ac853e6fdf3cb114659b231b344da018c3a0793ceca7f` |

The original stopped sender still has image 8bc3de1. It has **not** been replaced or resumed. The prepared forward image is available locally; image presence is not an activation claim.

The compatible source preserves the later production default-off conversation controls and rolling 2,000 replies per creator/fan/24 hours, including lifetime counters and in-flight protections. The targeted caption revert, native AI-05 final-message validation, explicit takeover controls and uncertain-send holds remain. No prompt or caption redesign was added.

## Completed evidence

[Production evidence](evidence/production-release.json) records exact images, hashes, timestamps, financial observations and backup proofs. API, reader and broker each match all 712 staged Python file hashes. Creator sign-in matches the committed Git blob SHA256 `2678c63ef05abaea2d4471e70ae24b04dcebaafbc624ffd89d0801339b77452f`; the staged Windows file differed only in CRLF line endings.

2,271 acceptance cases passed with zero failures, errors, skips or deselections in the accepted aggregate, alongside both native suites and two required isolated scripts. The unchanged pinned aggregate checker passed. The original run 40553797469 remains red because its legacy lifetime-counter fixture inherited daily mode. Its original failed artifact is preserved. Full shard 0 was rerun against the same application commits in run 40560725239 with explicit legacy mode; shards 1–5 and native results came from the original run. This is combined evidence, not a claim that the original workflow was all green. Artifact digests and provenance are included.

The explicit test-fixture correction, published separately from runtime source, passed 17 recovery/takeover/daily checks with surrounding daily mode 2000. Actual-image focused checks and the populated restricted rollback rehearsal passed. Earlier 2,265/2,260/2,182 evidence remains separate.

## Production operations

Admission and writers were stopped before backup. The sender exited cleanly. The old broker ignored SIGTERM and was force-stopped only after its full timeout, after other writers were stopped; no OOM occurred. Its new deployment uses SIGINT for orderly shutdown.

Fresh authenticated encrypted backups cover CRM, proxy, FASTT runtime, owner sessions and runtime configuration. The isolated restore matched 161 data tables and **829,342 messages**, and remained quarantined with release prohibited. The first isolated restore attempt failed; the successful retry used more resources for the disposable database. Production backup files and keys remain on the original server. The encrypted CRM archive is approximately 243 MiB, close to the tool's 256 MiB bound.

Production upgraded from `0093_conversation_default_off` to `0097_release_controls`. ANALYZE ran first. The initial 30-second statement deadline was insufficient and the transaction rolled back. The successful bounded retry completed the migration in **77.413 seconds**; unchanged-table fingerprints matched and content-source backfill added eight rows. Temporary database CPU/memory increases were restored to their original limits afterward.

The additive session SQL was applied only to Supabase project `aouaenqayfltoqrmpitg`. Existing two owner accounts and nine session records were unchanged. RPCs use security-invoker, empty search path and service-role-only execution. Public HTTPS login, actual logout, rejected cookie replay and preservation of a second session passed. All created test sessions were revoked. Legacy HMAC compatibility remains off. The first loopback logout probe failed Next.js's forwarded-origin check; the public HTTPS test passed without changing application security.

## Actual remaining blockers

1. Fresh provider ledger coverage has not completed. Nicky (`of-176774528`) reported `provider_rate_limited`, respected its backoff and subsequently resumed reading. The latest [financial follow-up](evidence/financial-followup.json) still reports incomplete coverage and four current-binding unlinked purchases for the enabled affected account. All readers retain their normal bounds.
2. Enabled account `of-323358988` has **four unlinked purchases occurring after its current binding began**. Their saved provider observations contain no message ID. Resolve them only with exact provider evidence through the existing audited ledger-resolution path. Do not guess links from matching price/time.
3. Recheck financial readiness, then execute the prepared sender activation and post-deployment checks. The full messaging release has not passed this gate.

Historical unlinked purchases remain visible but do not themselves hold the current binding. The release verifier now explicitly counts purchases occurring after binding creation rather than treating the diagnostic total of imported historical purchases as an activation failure. Commerce holds, conflicts, freshness and current-binding purchase checks all remain enforced. The disabled account remains disabled.

## Continuation and rollback

Read the production evidence first. Reuse `operations/astra-verify-read-roles.py` after provider reconciliation. Only when `paidReady` is true and the public logout proof remains valid should `operations/astra-activate-release.py` restore the original API capabilities and replace/resume the existing sender. Then run `operations/astra-post-deploy-check.py` and observe genuine new input with provider-confirmed receipts. Do not create a second sender, enable extra conversations, clear holds, reset counters or replay uncertain sends. The complete final release must receive a new activation record; this held record is not approval to skip a gate.

Rollback keeps schema 0097 and additive session SQL. Use only the proven schema-compatible read-only API/dashboard rollback, with command credentials removed. Old sender, worker and sign-in writers must remain stopped. Never restore the old database over newer receipts.

The controlled paid-provider/model journey remains unverified. NativeEngine remains serial; 250 creators and guaranteed 120-second acknowledgement are not proven. AUTH-02/03 Flirt pairing remains deferred.

After Jesse explicitly approved both destinations, all four backup objects were uploaded to `s3://twin-worker-transfer-38e051d1/astra-compatible-20261028/production-backup/` and read back with matching SHA256 checksums. See [remote retention proof](evidence/remote-retention-proof.json). The earlier held-release snapshot preserves the prior approval block as historical evidence; it is superseded for remote backup status by this proof. Keys were not uploaded. Jesse also explicitly approved publishing this release evidence to twinagency/twin-onlyfans-backend.
