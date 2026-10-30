# Contextual offers release record

> Superseded operational status: recent sync is repaired and Maile is in Preview. The independent reply-pacing release is now deployed. See [sync evidence](https://github.com/twinagency/twin-onlyfans-backend/pull/60) and [overlap/compatibility reconciliation](https://github.com/twinagency/twin-onlyfans-backend/pull/61). The image table below is a historical release record, not the current runtime.

30 October 2026. This supersedes the pre-deployment status in RELEASE.md, live-baseline.json and validation.json.

## Actual status

| Stage | Evidence |
|---|---|
| Implemented | Backend PR #56; dashboard PR #335 |
| Targeted CI tested | All five PostgreSQL release groups pass at backend `0b4e7eaf0afd87846723f134313d5db2eb78727f`, run 41011316445 |
| Production-image tested | Grafted sender: 47 contextual cases; four synthetic native turns, fixed/dynamic prices; PostgreSQL load/takeover 17 checks; populated migration rollback passed |
| Deployed | API, worker, supervisor and Twin dashboard; immutable identities below |
| Enabled | Off for all creators. No shadow/live cohort selected or activated |
| Provider contextual send verified | No |
| Purchase/unlock verified | No |

No real fan test message, test purchase, new fan activation or account reconnection was performed. Existing on-request settings remain enabled where previously enabled. Nicky remains paused, runtime disabled, revision 10, disconnected_by_owner; Katie remains paused, runtime disabled, revision 4. Alva's configuration was not repaired.

## Deployment identities and source reconciliation

Host `twin-dashboard-prod`, instance `i-09a9dd3bd749643a5`, ap-southeast-2. Deployment SSM command `eff02998-bef0-4c69-9453-10cc8bd4f0e4` succeeded. Schema upgraded `0100_manual_free_media` to `0101_contextual_offers`. Health and explicit schema check passed.

| Process | Running immutable image ID |
|---|---|
| API | `sha256:cf7dd2382962def7ca18f8b26a39713e61cab5bf5bb637093ce8bf95068527ff` |
| Worker | `sha256:3257dba4b75a0a0bbf43a9a0b99a17e04a0af11f76b4105a770b9667fd17c0df` |
| FASTT supervisor | `sha256:42a374a0e29fa08419c2ec4987299d187215a28066528a48d1ac94c79a97e84a` |
| Dashboard | `sha256:99c570f226a2bebd435af40cee5ef50072d30ffb5b95e3a3eb26fdd7aa880fc1` |

Backend feature source revision: `0b4e7eaf0afd87846723f134313d5db2eb78727f`. Dashboard revision: `c3576f07f90a8197c3b5f95a9e62e093b827415e`. These are feature revisions, not a claim that the entire production source equals a historical repository checkout.

API's touched source matched attribution PR #55 before patching. Sender contained additional deployed engine-v2 concurrency, send-gate, open-offer conversation and memory repairs absent from that branch. The patch was applied by three-way merge onto that actual sender source. The single constructor conflict retains BOTH `open_offers_ok=False` and `contextual=None`. Other deployed engine-v2 modules remain in its immutable parent image. The worker received only the three schema/model/migration files. Attribution behavior and sender-specific fan-name/memory overrides remain intact.

Exact on-host merged source and git history are retained under `/var/lib/twin-release/contextual-offers-20261030-astra-6172d27/sender-merged`. API and sender image parents are respectively `49cf1882099727ab3a6447f3df082c24343572ac4f800242f3f26362cd54b472` and `deff89ed978f4e59236387abab1c4bde85aed0e649e0fe23f1a10b009ae47bf2`. Worker parent is `eb49d6dac1aafa4167a11625256ebaecfb06a5a4f7681c8e38b3049317a64579`. Full sender source was not exported: automatic approval review blocked that SSM transfer. Tests instead ran directly against its rebuilt image with synthetic fixtures and isolated networking. The PR must not be used later to replace the complete production sender tree without this reconciliation.

## Changes and compatibility

Configuration and audit validation add `fastt_contextual_sales_mode`, API field `fasttContextualSalesMode`, off/shadow/live. Existing creator editor exposes Contextual offers: Off/Preview/On. The extra GET field requires `?contextual=1`, preserving older dashboards' strict v3 parser. The new dashboard requests it. Live read confirmed legacy field absent, negotiated field off, and existing on-request permission true on Maile.

Existing ai_chatter/SellLane/resolver/pricing and native intent/lease/receipt authority own delivery. Separate contextual relevance, grounded media matching, frozen revisions/prices/previews, durable original-turn/media identity, unresolved contextual-origin hold, takeover/off rechecks, and ignored-offer follow-up suppression are added. No unconditional inference, new sender, timer, persona retraining or activation expansion. The initial implementation chooses ordinary chat or a matched paid card; suggestion-only messages are not emitted.

Migration adds `of_contextual_offer`; it does not mutate existing configuration or activation. Promotion compared exact protected configuration, runtime, connection and takeover snapshots before/after migration and found equality. Zero contextual origins existed at live verification.

## Test results and limits

Release run https://github.com/twinagency/twin-onlyfans-backend/actions/runs/41011316445: contextual (47 unit cases, four native turns, 48 PostgreSQL configuration/migration cases), manual (35), ledger (92), historical upgrades (3), native load/storage all passed. Local SQLite portability correction preserves the PostgreSQL JSONB media-shape constraint and allows existing runtime checks to execute.

Actual sender-image regressions also passed ledger (300), runner (262), offers (31), follow-ups (45), post-sale engine (278), resolver seam (21), model (14 tests), identity and manifest (23). Actual PostgreSQL load produced 12 synthetic replies, respected throttles and refused in-flight takeover sends; all 17 checks passed. Synthetic fixture constructor keywords were updated to match existing production capabilities.

Two historical sender fixtures remain red: daily_allowance and service pace-stop event expectations. Both reproduce on the unchanged pre-release sender. The attempted receipt-only fixture correction did not fix daily_allowance and was not published. Production allowance/send-gate behavior was not weakened to satisfy it. General repository CI also has unrelated combined-acceptance harness/source issues; the full repository is not represented as green.

Dashboard deployment run https://github.com/twinagency/twin-dashboard/actions/runs/41011459654 passed type/build, upload/session checks, serving SHA, private-media ownership and disposable upload persistence checks. Its final all-creator catalogue audit failed on four invalid video probes (1309/1313 passed); the same four failed in the preceding release run. The deployed image remains running. This is not a clean all-catalogue acceptance result. No physical-device or authenticated operator walkthrough was completed for the new selector.

## Purchase sync and enabling block

Read-only live verification at **2026-10-30 06:20:25 UTC (16:20:25 Brisbane)**:

| Creator | Recent offset | Last successful completion UTC | Current window end UTC | Error |
|---|---:|---|---|---|
| Maile | 2640 | none | Oct 29 11:04:53 | provider_read_deferred |
| Alva | 2420 | none | Oct 29 11:51:04 | provider_read_deferred |
| Naomi | 160 | Oct 29 11:42:23 | Oct 30 05:04:16 | provider_read_deferred |

Since 05:26 UTC, offsets advanced 120, 120 and 140 respectively. This shows scan movement, not fresh completed coverage. Total remaining backlog is unknown. Purchase state does not meet the guarded 300-second existing poll interval plus 45-second read lease threshold. Contextual activation is therefore blocked. No shadow traffic, ignored/no-match live cases, canary send receipt or purchase/unlock was manufactured. A deterministic lease-contention case demonstrates occupied lease with remaining tokens and no unsafe overlap; recent-head scheduler fairness still needs a separate reviewable repair and load verification. No scheduler repair or provider-budget bypass was deployed here.

## Kill switch and rollback

Keep `fasttContextualSalesMode: "off"` through the existing audited configuration API/editor, preserving revision/fingerprint/idempotency semantics. Native transport rechecks it, so queued contextual actions lose authority before posting. On-request sales remain independent.

Fresh baseline: `/var/lib/twin-release/contextual-offers-20261030-astra-6172d27/baseline-0b4e7ea`. Protected schema/config/control backups and exact original compose: `promotion-0b4e7ea/` in that same root. Parent images are protected by retained, non-running artifact containers against image pruning. The baseline compose SHA256 was `b0837e09491a16aaa418ff44f461e8e5bc840fbe8c27bb4d29ed0b4e4d8ac510`.

Current rollback: set Contextual offers Off through the audited settings API/editor and retain contextual evidence. Do not restore this release's whole compose or original API/sender images over the later pacing release. Use the current per-service baseline and narrow rollback in PR #61. The historical schema downgrade and full-image rollback were tested at this initial release, but are no longer a safe unattended rollback after subsequent changes. Never start the disabled legacy fastt-runner and never resend delivery-unknown attempts without reconciliation.

Dashboard original baseline is `sha256:b3d710711da8566d1eae821d11ee38022f5b6833f68e8d1a9774c23bb0a8793a`, revision `d1723b44a1c4c1d0e89a934f3498e32f8c0b3aba`. Preserve each container's captured environment, network, ports and mounts when restoring. FLIRT's separate older release was not replaced; negotiated API fields preserve its configuration reads.
