# Recent-history onboarding release, 2 November 2026

**Maile's backend pilot is deployed and running. AI replies are confirmed disabled. The pilot and full dashboard release are not complete.** No fresh onboarding duration, 90-minute result, or numeric time/cost saving is claimed.

## Current deployed release

| Item | Verified value |
| --- | --- |
| Application/build source | `9870b64889833b16a36397c8ffa45dc7e236cea3` |
| Deployment scripts | `be42c3120aa75c402514a1dbeef89b0908da230e`, packaged from `21edbb880a563748db71a38d9b81a38be5b3fd32` |
| Worker, importer, learner image | `sha256:9a70ed1001e221fc4a9171112b3ac49112cfa7cdbe11dd209e0267e59271330e` |
| API image | `sha256:a0aa29f506aa0f81f1a4cfb275ca7d185ce1a771f9489c91254311ac28e87934` |
| Schema | `0110_recent_history` |
| Pilot account | Maile, `of-323358988` only |
| Deployment completed | 2 November 2026, 19:44 Brisbane |
| Policy start / fixed cutoff | `2026-11-02T09:44:23.165525Z` / `2026-10-03T09:44:23.165525Z` |
| Existing provider capacity | 2 requests/second, 8 in-flight slots; unchanged |
| AI controls | Maile paused through existing audited control; reply supervisor remains stopped |
| Dashboards | Both original containers and images preserved |

The API, worker, importer and learner were read back running on the expected revision-labelled images with zero restarts. The additive production migration and API health check succeeded. The actual compiled OF dashboard label passed two checks; its legacy response cannot label an incomplete lifetime archive complete.

Private pre-pilot backup: 750,543,194 bytes, SHA256 `8dd2fcd543acbba784c8c8e1b2d4c268cfcf90739489bcfbeff323f207621337`. Configuration/container snapshots, backup and rollback configuration remain on the operational host. No database restore or destructive migration was performed in production.

## Reporting repair and measurement limitation

Reporting repair **`c8216d33ef409be5bc99aed0f654b1ccfe3023b3` is committed but not deployed**. It addresses two observed failures:

1. The initial promotion's `docker run` omitted `-i`. The Python stdin baseline command did not run, leaving an empty snapshot. The original deployment manifest's `baselineCapturedBeforePilotJobs=true` claim is incorrect. `evidence/pilot-deployment.json` corrects it. Future promotion attaches stdin and validates the complete JSON snapshot before starting pilot jobs.
2. The progress receipt query omitted the tenant column at the front of the receipt primary key. On Maile it hit the existing 10-second statement timeout. The tenant-scoped query completed in **1.068 seconds** in a read-only production check. The repair also scopes measurement receipts and error/hold reporting to the tenant. A new PostgreSQL regression verifies that another tenant's receipt cannot complete this tenant's memory.

The intact pre-pilot dump can supply source, receipt and model-call evidence. `recover-pilot-baseline.py` restores it into a disposable PostgreSQL container on an internal network with no production credentials or external access, migrates that copy, applies the live frozen cutoff only to the copy, and produces private fingerprints. It keeps the original empty snapshot and records the dump snapshot's lower/upper timing bounds and live-arrival gap. **Recovery has not been confirmed executed.** A reconstructed result must never be presented as an original start-of-pilot capture or fresh onboarding benchmark.

The repair artifact was prepared and the remote staging command was attempted, but its submission/completion is unconfirmed after the control device stopped responding. A subsequent AWS list returned no matching `source-c821` command in the latest 20 commands. Do not resubmit blindly: inspect command records and host files first. No reporting-repair promotion command has been submitted.

- Repair artifact SHA256: `38ccaab934c9981e2e3ede85edc7951054082501d93e44495049169ff6c98ba8` (15,459 bytes).
- Intended S3 key: `onlyfans/releases/recent-history/c8216d33ef409be5bc99aed0f654b1ccfe3023b3/repair.zip` in the existing private transfer bucket.
- Intended host source: `/var/lib/twin-release/recent-history-20261102-resume/source-c821`.
- Intended qualified build: `/var/lib/twin-release/recent-history-20261102-resume/repair-c8216d33ef40`.

The last small readback showed 6,648 recent conversations needed and two running; discovery was still running without a recorded discovery error. These are an intermediate cursor checkpoint, not complete fan coverage. A separate query counted 30 incoming sources still lacking matching receipts. Neither observation establishes completed memory or onboarding.

`MAILE-MEASUREMENT.json` distinguishes these facts from the earlier provisional stored-archive snapshot. Final recent-window fan/message counts, provider requests, import duration, memory duration and new actual model charges remain unmeasured. The older 87,646-message / 16,571-fan observation used a different provisional cutoff and is not the pilot result.

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

## Checks and release continuation

- Initial deployed exact-base candidate: **66 passed**, 333.14 seconds pytest / 336.525 seconds wall. SSM `db69d054-c32c-44a7-87b0-3934d88765a7`.
- Original promotion: SSM `7ede05c8-6637-403e-8461-8c45f6bc0ba7` succeeded, including backup, audited Maile pause, migration, health and actual-dashboard label checks. The empty-baseline defect is explicitly corrected above.
- Reporting repair local PostgreSQL: **22 passed**, 44.28 seconds pytest / 46.332 seconds wall.
- Earlier broad/targeted PostgreSQL checks: 211 passed plus later 56 and two capacity cases; counts overlap and are not a distinct-test total. Local compatibility 57 and earlier measurement three also passed.
- Four component Chromium checks and 12 strict-parser checks passed. Full live frontend build remains unverified.
- GitHub runs ended in `startup_failure`; no GitHub CI pass is claimed. One stale ledger fixture also fails on the untouched original baseline and remains explicit; this change does not qualify the separate concurrent-reply R9 gates.

On restored control access:

1. Inspect AWS records and the intended repair directory before retrying staging. The initial deployed images above remain the last verified live state.
2. Run the committed reporting build/qualification and isolated baseline recovery if not already completed. Never reset pilot cursors or delete imported data.
3. Require a passing repair qualification and a valid reconstructed baseline before invoking `promote-reporting-repair.py`. It checks image/configuration leases and release locks, preserves all environment values, cutoff, stopped supervisor and dashboards, and makes no schema change.
4. Run the committed `measure.py`. It keeps private fingerprints on the host and publishes only aggregates: unchanged reused sources, new/edited sources, reused/new receipts, history-page canonical additions, HTTP attempts, and new calls separately from pre-existing-call settlement changes. Provider-reported charges, token estimates and active/unresolved reservations stay separate. Receipt span and cumulative model latency are not end-to-end memory duration.
5. Declare recent readiness only after successful discovery and every relevant thread. Expand only after Maile's coverage, controls and measurement checks pass. No expansion is authorized by this checkpoint.

## Rollback

A private schema-compatible flag-off configuration is retained. Stop pilot importer/learner work, remove Maile from the rollout allowlist on cooperating services, and retain compatible binaries and all data/cursors/receipts. Turning the flag on again reuses the fixed cutoff and progress. Old binaries pinned to schema HEAD `0106_parallel_history_reads` cannot be used as an image-only rollback after `0110_recent_history`. The migration refuses destructive downgrade. Never automatically re-enable Maile replies or start the stopped supervisor.
