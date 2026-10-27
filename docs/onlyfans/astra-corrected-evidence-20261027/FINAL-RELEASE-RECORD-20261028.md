# Twin CRM final review record — 28 October 2026

**Review packet complete; production release HOLD.** Final testing passed for the exact selected source pair. A separate deployment changed production while that run was executing. The selected candidate must not replace it: the new live schema and runtime patches are not in this candidate. This is an explicit authority/schema compatibility hold, not a test failure or a request for another midpoint review.

## Exact tested pair and artifacts

Backend application source: 1c2030eb5fb1a3af7df166019a771dd8f5528f2a. Dashboard: 4599999761dbf2ff2b517359642d10262324867a. Dashboard source archive SHA256: 1accd652bcca3c274b569ac72ac6ec6910dd20508d477d665527fb6bced31c60. PR #33 contains CI/evidence supplements beyond the selected backend; those supplements do not change its application files, and tests are attributed only to their actual source.

[Final acceptance run 40541003637](https://github.com/twinagency/twin-onlyfans-backend/actions/runs/40541003637) completed successfully, including both native suites, all six acceptance shards, the two isolated scripts and aggregate. **2,265 pytest cases passed, with zero failures, errors, skips or deselections.** Both isolated scripts, service/tests/test_onlyfans_read.py and service/tests/test_onlyfans_migration_pg.py, exited zero. SQLite native: 29 declared commands; PostgreSQL native: 7; all exit zero and report zero skips. Native checks are separate from the 2,265 aggregate and are not double-counted.

| Shard | Passed | Failed | Errors | Skipped | Deselected |
| --- | --- | --- | --- | --- | --- |
| 0 | 472 | 0 | 0 | 0 | 0 |
| 1 | 359 | 0 | 0 | 0 | 0 |
| 2 | 322 | 0 | 0 | 0 | 0 |
| 3 | 288 | 0 | 0 | 0 | 0 |
| 4 | 356 | 0 | 0 | 0 | 0 |
| 5 | 468 | 0 | 0 | 0 | 0 |

The conditional repository-checkout step was unused because the exact verified private archive was selected; this is not a skipped test. Artifact-only jobs were disabled in this test run and ran separately below. Original assertions, case identities, commands, archive digests and paired-source provenance are retained in final-acceptance-summary-20261028.json and final-ci-provenance-20261028.json.

[Immutable artifact run 40541668140](https://github.com/twinagency/twin-onlyfans-backend/actions/runs/40541668140) built all six images successfully. Full identities, archive hashes, expiry and dependency/configuration inventories remain in final-immutable-images-20261028.json.

| Role | Producer image/config ID |
| --- | --- |
| API, worker, sender, broker | sha256:7578467ec1c0f216983b9b1c763284d5b86cd8a74cb2a9e23d45a1bebca6ba2c |
| Dashboard | sha256:98a82df874d61b744d11bf0e8635466f19a3e814abfb9d666c190a9b46f865d0 |
| Creator sign-in | sha256:f1dc62a4d44142648e1ed128de3e6797f61081fbf9ef0bd3770e79edd47bbc9c |

Backend archive SHA256 c9831c58f4ac8199cae043c0e95394db096ab8f0825344e4329c8422ca00ed95 was verified before loading. Docker 29 exposes loaded manifest ID sha256:174d20ef97087371f3ace2c4c267ae83ff908218a996bb652c88614c7411fc61 for the producer config ID above. RootFS layers and normalized configuration were verified identical; this is the same retained image content, not a second build.

Retained previously tested schema-0096 restricted rollback: backend sha256:49e446b89c828a38212c673d9710b011506fe722724a2955bedf4424ddbb5d3a, source d6811b124928f8167e6625ce9db5db81bf227fad; dashboard sha256:959107b653d83a2b30d41fc49ab5c40130f3cd696a1b4b274cb1f4a05c32e3d9, source 871b3e8955bf8af357f6f66d56c4446bf98fee8e. Actual-image populated 0096 rollback proof 40474649744 applies to these retained bytes only. Newly rebuilt rollback artifacts do not inherit that proof. It is not a runnable rollback against the current divergent 0093 schema, and writers must stay stopped during any restricted rollback.

## Completed stabilization corrections

The selected candidate preserves backup correction 12c7fed4, button-controlled handover b2a00c32, caption revert 8d89d1ff, native fixture ebd02c31 and packaging 3ac035bd. Deployed worker/API/sign-in fixes 9052698, 8a72ec0 and 4ea6bfa were inspected and already included as ancestors. No implementation was restarted.

The separate native/catalogue AI-05 path applies the existing delivery and unbacked-claim validation after complete reply-first description/price composition and finalize_draft. Only the exact validated card count prefix and price footer are excluded from the numeric-claim floor. Conversation and saved description remain checked. Valid output and appended delivery/invented-media/invented-price refusal cases passed: 44 focused native/media PostgreSQL cases. One model call remains. The restored FASTT named-request path has no additional caption call.

Dashboard browser expectation is Purchased media; financial assertions are retained. Focused browser verification passed 101 assertions across 48 page/viewport cases. Affected session fixtures now exercise the real admin-session RPC contract while retaining permission, brand isolation, revocation/replay and outage assertions; five additional fixtures were repaired only after reproducing their missing-RPC failure. No production auth permission redesign was made.

Conditional triage retained existing bounded maintenance/failure isolation, financial status/identity/conflict fail-closed behavior, server-only broker management credentials, and durable vault continuation/frozen inputs. NativeEngine/global-hook serial capacity remains a disclosed limit. No new scheduler, polling system, vault ceiling, prompt feature or scaling project was introduced.

## Caption correction completed; current live state is newer

The targeted caption-only correction was deployed and verified at 2026-10-27T16:04:59Z (02:04:59 Brisbane), application commit 8d89d1ffedc860648424c41707e89908497d3bc5, image source 3ac035bddf0f7cd29285d7ad5d194b902def1f13, image sha256:80c488cc3fb9dd974d8c743c36be0cbb627468af6192b289d76723a9c2fd9b7c. It used safely fenced replacement, canonical compose, reproducible committed image, retained rollback and preserved receipt/grant/unknown evidence. At that deployment all 656 Python files were verified.

Latest independent read-only check: 2026-10-27T17:20:01.380485+00:00. The sender now has 658 Python files. Exactly seven Python paths differ from the completed-caption baseline, all listed with old/new SHA256 in final-review-production-fingerprint-20261028.json. None of the four caption runtime files differs; their current verified hashes are:

| Live caption runtime file | SHA256 |
| --- | --- |
| automations/content_prompts.py | 6985fba7c32efe7c00d47800a6cd2e939275380a9d8af93819bbebdf81eb216c |
| automations/content_resolver.py | 4de0ee4a2f936989f67dbec81d09403abecb5086dc2330379cd73ef776c1eeab |
| automations/pack_claim.py | 121f0fcafe97106d2a819214b7afc596016336767d91760e935e78458236d385 |
| automations/pack_sender.py | 793645f1a99e73392e70a25dd5af763d3d7961d18f64ae892cc4a41d3fc5a5a4 |

The button-handover runner hash remains 955a6fe241afdeae35a5690dfdf1bd74a8108893d7d0067c77920f117c25d8fe. The later service diff is the daily-allowance call-site change; it does not restore outgoing-message automatic takeover.

A separate deployment at 2026-10-27T17:13:32Z installed default-off fan controls and the earlier 2,000-per-fan rolling quota. Its own record explicitly states that live source differs from its Git revision label:
- [Default-off patch/record commit 1965c39a](https://github.com/twinagency/twin-onlyfans-backend/commit/1965c39aeb11716dd3d796e54b87f015b8dfa791).
- [Daily-quota patch/record commit 39ee91b4](https://github.com/twinagency/twin-onlyfans-backend/commit/39ee91b41f7a01a79b372479f50153f6b2261d9d).
These commits archive runtime patches; they are not complete consolidated image sources. That deployment also changed registered-creator scope and new-conversation policy through its own audited commands. This continuation did not execute those changes, reset counters, widen scope, change credentials/models/personas or send provider messages. Preserve the actual current authorized configuration during reconciliation.

| Current role | Live image |
| --- | --- |
| api | sha256:f46ac38ec5e3821940c880f911d23f2d8a2d0925aca688c17abd86f4b0c89881 |
| fastt-supervisor | sha256:8bc3de180159ffd94e1318fa376826439389a79cefe2677ba4790b007e18d024 |
| worker | sha256:f9f72d1ef5ea8605ae4eeb0835dd07046d75e10f3f32be74044211a0a44b49f3 |
| creator-sign-in | sha256:d2410f7779c8e70f4f92176e5c3df551743623074d834e4974fc5d75a948b454 |
| managed-proxy | sha256:a0b2c927824d89b7ca22e27908cd7fede9cfd652631499136711c820e55088d5 |
| database | sha256:ca0bd484cb98bf4b24eb1010e73fb3fcbd6714d240fbc1a10eea5b7dbecb641d |

Current actual database revision is **0093_conversation_default_off**, with one unresolved attempt still held. It is a different branch from **0093_ledger_markers**. Candidate 1c2030e recognizes neither the new revision nor its live default-off migration; its backup tool explicitly accepts only 0092_ledger_history and its own HEAD. Deploying it unchanged would refuse startup/backup and omit the new quota/default-off code. **Do not execute the old 0092 backup/migration packet, relabel the source revision, stamp backwards, drop the new migration or overwrite the live files.**

## Backup execution environment and bounded rehearsal

The candidate application image lacks PostgreSQL client binaries. A separate immutable recovery-tools image was built from the retained candidate plus exact deployed PostgreSQL 16 binaries/libraries, without modifying a live container:
- Image sha256:6487b89c9ac5d6081701cd6bf5430ba3b6c5c79a659ce6de0c6234991955c1dc; archive SHA256 b543ea18bdc4a4518de9a77774a6ddebc1d24f03b0c3ca59f9ff7865e5a41d8a.
- pg_dump, pg_restore and psql version 16.15; wrapper directory /opt/pg16.
- 17 actual-image recovery tests passed with no skipped/error/failure cases, including encrypted public+onlyfans_proxy backup, genuine 0092 pre-upgrade source validation/refusal, isolated authenticated restore, independent quarantine and subsequent migration.
- Recipe, identities and proof: final-recovery-tooling-proof-20261028.json. This proof applies to 0092/candidate HEAD; it does not certify backup of the newly divergent 0093 source.

Updated isolated representative rehearsal used 850,000 synthetic messages, above the observed production estimate of 794,228. With five-table ANALYZE, 5-second lock and 30-second statement limits, the 0094→0096 pending-source upgrade completed in 14.813s. Discovery p95 2.499ms over 30 samples, partial index used; runtime authority preserved, zero send intents/financial rows created. It used an internal disposable PostgreSQL database and no provider calls. It proves the heavy pending-source segment, not the newly deployed schema branch or full production latency targets. Details: final-850k-rehearsal-proof-20261028.json. Host free storage was 103 GiB at the final preparation check.

## Explicit release decision and next execution gates

**HOLD the full CRM cutover.** Keep the currently running application and its new controls. Astra's agreed final review must include this newly discovered compatibility gap. The concrete remaining work is to consolidate only the two already-deployed runtime patches into the final candidate, reconcile the migration graph from actual 0093_conversation_default_off without editing/stamping production, extend strict encrypted backup/restore source validation for that genuine source, and verify the preserved quota/default-off/hand-over/unknown-attempt behavior. Re-pin and rerun only the checks/artifacts invalidated by those code/schema changes. The 2,265 green results above remain evidence for 1c2030e/4599999, not for an unbuilt successor.

After that compatibility hold is cleared and Astra completes final review, use the existing controlled maintenance procedure: close admission; fence/drain the persistent sender; stop all writers/restarts/late sign-in; take fresh encrypted CRM+onlyfans_proxy backup and separate session-store recovery material; verify quarantined isolated restoration; bounded ANALYZE; explicit compatible migration chain; broker recovery init; correct session SQL; start reviewed roles with outbound fenced; verify exact live source/config.

Twin/Moxy session store is aouaenqayfltoqrmpitg; Flirt's lkmfwpqegjluirfusnaw is separate. Apply dashboard-session-revocation-upgrade.sql to the correct store before code switch. Preserve owner/agency rows, leave legacy HMAC compatibility off and require admin re-login.

After the ledger migration, require account-current binding/connection coverage, lastCompleteAt within 15 minutes, complete traversal and no blocking conflicts before paid sending. Do not accept timestamp-only readiness or unavailable totals represented as zero. Preserve all newer receipts/counters/unknown attempts.

Refresh the [existing controlled-provider packet](../astra-controlled-provider-verification.md) immediately before use. Through the persistent dashboard-managed sender only: one manual text, one approved paid item, one AI reply to genuine fresh controlled input. Confirm actor, account, recipient, revisions, proxy, model, budget, media and price. No second sender, fabricated input, forced purchase or uncertain retry.

Production-window backup/restore, migrations, session/broker cutover, financial reconciliation and controlled live journey have not been performed by this continuation. Form 300ms, list 500ms, inbox 2s, thread 1s warm/2s cold, list visibility 5s and eligible AI acknowledgement 120s remain unmeasured live targets at the intended starting workload. No 250-creator capacity claim or wider authorization follows from synthetic tests. AUTH-02/03 remain jointly deferred.

The [existing 36-ID register](REQUIREMENTS.md) is updated, not replaced. Completing the caption revert and completing final selected-pair tests do not mean the wider CRM production release is complete.
