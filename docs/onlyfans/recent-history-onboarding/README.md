# Recent-history onboarding release, 2 November 2026

**Current backend release: 9d6756b, deployed and healthy. Maile only; AI replies disabled.**

[Current deployment, measurements, 79-test qualification and remaining gates](RELEASE-9d6756b.md). New-account enrollment is implemented behind a disabled rollout switch; other accounts have not been enrolled. Recent memory backlog is zero at the latest checkpoint, but provider coverage and the complete release remain unfinished.

## Historical 22:19 / 22:52 evidence below

The following counts and image references are earlier checkpoints, superseded by the current release report above.


[Latest checkpoint: memory backlog recovered; reporting correction published, not deployed](RECOVERY-2252-BRISBANE.md).

**Backend repair deployed; Maile pilot still incomplete. AI replies remain disabled.** No fresh onboarding speedup or 90-minute result is demonstrated.

| Release item | Verified value |
| --- | --- |
| Application source | 91c260ba54451ba51525cd3cc2ab0737bedc1592 |
| Worker/importer/learner image | sha256:b950fb0fb7514f4c4f20c298d1b84d124a37bcd940396dc1e51fb7de2778e319 |
| API image | sha256:60cbff853ce2142e58728429c8aa350def2ce7ee0f288babaca3e8a35c4ec265 |
| Latest repair deployed | 2 November, 22:19 Brisbane |
| Schema | 0110_recent_history, unchanged |
| Fixed cutoff | 2026-10-03T09:44:23.165525Z, unchanged |
| Control state | Maile paused; supervisor stopped; both dashboards unchanged |

All four deployed services were independently read back running on the expected images with zero restarts. Health, environment preservation, release locks and image/configuration guards passed. The controlled restart took 33.45 seconds. Rates, capacity and daily caps were preserved. Only Maile is opted in. The original pre-pilot backup and schema-compatible flag-off rollback remain private on the host.

## Measured checkpoint, 22:19 Brisbane

- Coverage: 90,476 stored window messages across 16,573 fans. Initial coverage incomplete; 23,897 recent conversation cursors not ready. Lifetime completeness is false.
- Reuse: all 88,539 pre-pilot window sources unchanged, no baseline sources lost, 2,714 completed incoming receipts reused.
- New work: 1,932 additional window sources in the comparison snapshot, including concurrent live arrivals and the backup-to-activation gap; 1,087 canonical additions committed by history-page imports.
- Extraction: 91 new completed incoming receipts, including 33 for reused sources. Current coverage query shows 2,805 of 2,835 incoming messages processed, with 30 pending.
- New calls: 58; provider-reported charges US$0.0085; zero token estimates, active reservations or unresolved reservations in that group. No pre-existing-call settlement changes were counted as new cost.
- Cumulative model-call latency: 67.553 seconds. Receipt commit span: 7,620.57 seconds. Neither is end-to-end memory-processing duration.
- Provider attempts: 5,926 in the comparison snapshot; 5,935 in the later coverage query. Separate reads and concurrent activity explain differing totals.
- Policy elapsed: 9,322.85 seconds, approximately 2h35m, and still incomplete. This includes live operations, validation work and restart time on an existing archive; it is not a fresh-onboarding performance result.

The original baseline file is empty because the initial promotion omitted Docker stdin attachment. Baseline evidence was reconstructed successfully from the verified private pre-pilot dump. Its transaction snapshot is bounded between 19:42:23 and 19:43:45 Brisbane, before the policy start at 19:44:23. The gap and concurrent arrivals are explicit in the aggregate measurement. It must not be presented as an original start-of-pilot capture.

Offline recovery uses a disposable internal network and synthetic DB credentials. Long-read timeouts apply only to that isolated database. Production timeouts were not changed. Successful recovery cleaned up the temporary database/network. Recovery tooling commits are 62191c352e4940cf7c2f9ae9c1709da3e37cac54 and d0d100cf9a4ecb02a62d61579ff51a41a284bdd0.

## Remaining blockers

Recent discovery is unfinished. Provider failures remain visible: the checkpoint records 208 protocol failures, nine timeouts, one transient HTTP failure and one conflicting-history cursor. Bounded post-deployment diagnostics identify HTTP 404 for inaccessible message threads; these are not successful coverage. Initial readiness must stay false.

There are 14 memory jobs with profile-model-request-failed and 30 incoming sources awaiting receipts. Preserve their existing retry/review controls and budget. Creator onboarding reports review_required; no creator approval is fabricated. The exact running dashboard source remains missing, so the new card is not live. No rollout expansion has occurred.

The next work is to resolve provider accessibility/coverage and model-request blockers, complete the existing onboarding approval path, and recover the exact dashboard source. The actual end-to-end import/memory completion time and full-history cost/time comparison remain unknown. Stored incoming-character volume in the window is about 80.9% of the stored incoming archive; this is source volume, not a measured 19.1% billing saving or a lifetime estimate.

## Source integration and dashboard provenance

Dedicated branch: `codex/recent-history-onboarding-20261102`.

- Backend PR: https://github.com/twinagency/twin-onlyfans-backend/pull/78
- Dashboard companion PR: https://github.com/twinagency/twin-dashboard/pull/345
- Original/concurrent lineage: `d3a702262abd918588587c12f67c5fb9611100d7`, with later `cfab868e2c87706ddce4ff6cfb2bbeffd76c720f` integrated.
- History/capacity lineage: `e3f033c3bffcd037696a82e79ef50ccc86e939c6`.
- Shared base: `7616ec92bf01a6b7f3beb45bd45b943309c17285`.

Production builds apply committed task-only contexts/additions over the exact existing immutable API/worker images. They do not replace the whole conversation engine from an older branch. Context/hash mismatches abort. Existing production overlays, concurrent reply work and Moxy remain separate. Alembic merge `0109_history_release_merge` joins the existing migration lines before additive `0110_recent_history`.

| Running dashboard | Identity |
| --- | --- |
| OF image / ECR digest | `sha256:22be6bc89bf7308e2c2214e03d63313bfc32f2edb6c831bddcd1a06a28258049` |
| Reported OF source | `3d0ee2e3e2848f9cd5019399a82fe27a67f435c2` |
| Next build ID | `xvlzKe6xnQRSavfQQmgfM` |
| Image creation | 1 November 2026, 14:39:35 +10:00 |
| OCI source/revision labels | Absent |
| Separate main dashboard image | `sha256:ab10c2335b9771af12403b64a7db9982e134ac5e421d76b14840fedcb4fca9e4` |
| Main dashboard reported source | `2c6ac02f8b76b5cc6d22a690dd5fb047f3cc6844` |

The exact OF Git object still returns 404. None of 357 inspected branch heads matched. Existing release directories retain environment/previous-image records, not the source; the located checkout is substantially older. Available CodeBuild/S3 records and the other release host did not recover the matching build context. The reported GIT_SHA alone is not source-provenance proof. **Missing:** the Git objects or protected source archive/build context used by the original builder of this exact image.

The backend now adds `recentHistory` only when requested with `history=recent`. Legacy callers receive the original exact shape. For an incomplete lifetime archive, the projected legacy `sync.recoveryRequired` is true and `lastCompleteSyncAt` is null; stored timestamps and lifetime cursors are preserved. The real compiled label function was executed, not inferred from an old branch.

The new dashboard card has four component-level Chromium checks and 12 parser checks, but no qualified complete Next build on the matching live source. It is **not live**. Smallest safe route: finish the compatible backend pilot while preserving both dashboards, then recover the original frontend source and integrate the card there. Do not reconstruct the frontend from the older companion base.

## Policy behavior

The deployment allowlist `ONLYFANS_RECENT_HISTORY_ACCOUNTS` defaults to `[]`. The durable policy freezes a 30-day cutoff per current binding. Reconnection/restart preserves it. This is an import boundary, never a deletion policy.

Independent recent scans/cursors preserve lifetime positions, messages, receipts, purchases, memories and manual corrections. Import persists both directions and canonical attachment metadata inside the window. Every thread is traversed independently. Discovery requires matching full ordered identity sweeps, followed by finite known-fan coverage and head catch-up. Live arrivals and overlapping pages deduplicate against canonical identities. Failed/inaccessible/interrupted pages remain incomplete; reaching the cutoff never completes lifetime history.

**Provider limitation:** the connector requests descending IDs, which do not prove descending timestamps. No supported date filter or snapshot cursor is assumed. The safe default exhaustively traverses ID pages and filters persistence by date. It may reduce extraction work but does not prove fewer provider requests or faster download completion. Date-based stopping requires explicit verified timestamp-order evidence, with cross-page validation. Stored validated lifetime coverage can terminate overlapping reads; arbitrary minimum stored IDs cannot bridge gaps.

Both paid `profile_learning` and deterministic `launch_history_memory` respect the fixed scope and activated older jobs. Bounded batches include outgoing context and reuse fingerprinted receipts, including fact-free results. Recent work precedes activated older work. Old jobs remain stored. The optional `ONLYFANS_PROFILE_ACCOUNTS` cohort restricts seeding, claiming, maintenance and reconciliation to Maile while preserving existing behavior when unset.

Approved onboarding uses the existing immutable review/approval workflow. It separates public biography/talking points, private information, writing style/examples, boundaries/approval requirements and pricing/operating notes. Private information is excluded from fan-facing prompts. Authoritative catalogue prices, purchases, entitlement checks and operating controls retain authority. Routine creator-archive extraction is disabled for opted-in accounts; an explicitly configured sample is bounded to 100 outgoing messages and still requires approval. Placeholders and mixed PG/PG-13 plus explicit-sexting instructions block approval rather than being resolved automatically. The conservative linter is not proof that every semantic conflict can be detected.

A returning fan's live inbound message activates one durable creator/fan older-history job. Repeated triggers preserve cursors and may raise priority for earlier references, commitments or missing purchase context. Stored older sources are reused before further reads. Existing bounded admission/backoff, aging and periodic older turns preserve live priority and eventual progress within available capacity. Chat text never establishes payment. The engine is instructed to use available evidence and the existing verification/review path; older history is not promised before the first reply.

## Verification and rollback

The deployed combined candidate passed 75 isolated checks on the exact immutable production bases: 336.37 seconds pytest / 339.89 seconds wall. The earlier c821 reporting candidate's recovered result is 67 passed. Earlier local 59-check reconciliation, real PostgreSQL lock rollback and final seven-case scheduler results remain separate; counts overlap. Upstream concurrent-reply performance results are not a live onboarding benchmark.

Promotion/measurement SSM command ebf8874a-de09-4cbc-b09a-e9d3513b5c35 succeeded. Baseline recovery 9f76aa26-3302-48b0-afe3-bfced8a56616 succeeded. The earlier waiting promotion 27fac695-70f5-46d3-9359-48d92d90c688 was cancelled before deployment. Raw aggregate evidence is in evidence/live-deployment-91c260.json and evidence/pilot-measurement-20261102.json. Private snapshots stay on the host.

GitHub CI startup failures are not passes. Four component Chromium checks and 12 parser checks do not qualify a full live Next build. The separate concurrent-reply release gates remain separate.

For rollback, stop scoped importer/learner work and apply the retained schema-compatible flag-off configuration. Preserve every message, memory, purchase, receipt and cursor. Old binaries pinned to schema 0106 cannot be used as an image-only rollback after 0110. Never automatically re-enable Maile replies or start the stopped supervisor.
