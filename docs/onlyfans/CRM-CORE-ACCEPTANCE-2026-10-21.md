# CRM core acceptance checkpoint — 21 October 2026

This is the current OnlyFans CRM checkpoint. The entire CRM is not complete. Read alongside DELIVERY-PLAN-2026-10-19.md; earlier checkpoints and synthetic tests do not prove live end-to-end acceptance.

## Current release: deployed and confirmed

- Backend PR12: https://github.com/jesse98/twin-onlyfans-backend/pull/12
- Integrated branch: `codex/onlyfans-agency-access-20261020`; PR12 merged at `6c09981a51b6034a11b95868eddf2ef1bd8bb91e`.
- Running API and worker source: `71532fe06132c2f641eefe67c756b5defa585550`.
- Running image on both containers: `sha256:3ee6c93965b1f73deef84c37642bfe4aa91a10281cf35c9d9d5ac2165d2a058e`.
- Deployment command `d3efd502-5103-4626-9642-da7b21ed5eee` **succeeded**. Its result was retrieved after deployment access was restored. Do not submit the release again.
- Schema remains `0088_native_vault_media`. Deployment confirmed preserved environment, persistent volumes and outgoing policy state, and no interrupted active media work or upload cleanup.
- The release includes PR11's renewable verified connection leases. It does not overwrite that fix with an earlier inbox candidate.
- No live fan send, model call, purchase, refund or upload was initiated for this acceptance.

## Implemented and tested

Verified connected native accounts can now load and periodically refresh canonical Fan/Chat/Message history while outgoing messages remain disabled and no model credential is selected.

The import-only worker skips model, planning, campaign, receipt-repair and delivery phases. Every read claim and page commit checks the current tenant binding, verified connection revision and expiry. Pausing, replacing or activating the connection while a GET is in flight prevents the stale page from committing. History queued before activation remains historical after activation and cannot create a reply.

Existing active-account behavior, bounded creator-cohort paging, send authority, financial evidence and ownership safeguards remain in place. This adds no schema, duplicate CRM store or dashboard layout change.

The exact final image passed **160 unit checks and 80 isolated PostgreSQL checks**. Both GitHub CI jobs passed. An unchanged frontend font-resolver failure passed on one retry. The prior broad candidate passed 120 PostgreSQL cases and exposed two fault-injection call-contract failures; preserving the existing normal intake call fixed them, and both cases passed in the final run. Earlier candidates `8710eee` and `3909945` must not be deployed.

## Live acceptance: provider read is blocked

Selected account: `of-176774528` (Jesse Creator Test).

The live containers run the tested image, but **real inbox import has not succeeded**. Independent read-only inspection found:

| Item | Observed |
| --- | --- |
| Saved connection | verified, revision 2 |
| Last verified identity | 12:30:34 UTC on 21 October |
| Saved verification lease | expires 18:30:34 UTC; renewable after successful verification |
| Connection health | `provider_connection_check_retry` |
| Outgoing runtime / policy | both disabled |
| Fans / chats / messages | 30 / 0 / 0 |
| Existing ledger transactions | 40 |
| Model calls / reply work / text intents | 0 / 0 / 0 |
| Inbox scan | running, 0 completed pages |
| Pending ingress / completed account sync | 0 / none |

The 30 fans and 40 transactions already existed before this release. They are not evidence that inbox history imported.

Two bounded diagnostic reads through the saved account connection and fixed proxy returned HTTP **503**, `text/html`, 21,853 bytes from the `/api2/v2/users/me` identity endpoint, before the chats request could run. The second read identified the Cloudflare server header. Neither a challenge header nor the checked challenge/proxy-error markup indicators were present.

This establishes a failure on the selected connection's provider route. It does **not** establish a global OnlyFans outage, prove a CAPTCHA is required, or prove the proxy alone is responsible. Do not ask for another login or change the proxy without further evidence. Preserve the normal connection flow and fixed account route; do not bypass provider verification. Normal bounded retry remains in place. Sending remains off.

Next live gate: the identity read must return a valid matching-account response, followed by observed successful chat/history commits and a complete scan. Increasing counts or one successful page alone is not full acceptance. Then verify the actual operator inbox and human takeover. No live recipient or paid transaction has been selected for a send/purchase/refund test.

## Earlier media work still present

- PR9/PR10 repaired curatable vault identities, larger bounded history decoding, and visible paragraph/link text extraction in history. Strict send receipts and purchase/ownership evidence remain unchanged.
- Last verified vault inventory: 1,064 assets — 515 photos, 491 videos, 55 audio and 3 GIF. All 1,006 photo/video identities passed curation readiness for that snapshot.
- 490 videos are DRM-only; one video had a progressive source. The status endpoint reports `protected_playback_not_supported` honestly. Supported protected playback is still unimplemented.
- Dashboard PR220's protected-player notice and progressive-player tests passed at 320, 390 and 1440 px. Later dashboard layout/performance work belongs to separate chats; this checkpoint does not assert their latest release state.
- The earlier all-creator catalogue audit covered all 30 creators: 1,313 videos, 1,309 passed and four previously identified damaged Briana originals failed. Do not erase or disable that gate to claim completion.
- Native ledger samples in the supported 90-day window parsed. Full scan and exact purchase-to-message linkage remain unverified; sampled rows lacked top-level message IDs.

## Remaining core scope

- Prove real native photo/video upload, individual batch outcomes, cleanup and authoritative vault refresh. Current source and synthetic tests do not prove selected-account success.
- Supply a supported protected-video playback path.
- Resolve the current provider read failure, observe complete inbox import, and prove operator inbox/human takeover on the authorised account.
- Finish complete audience import through canonical fans/conversations and campaign ownership. Creators without OnlyFans are not being migrated automatically.
- Verify exact purchase/refund linkage from explicit provider references, never amount/time guesses.
- Prove controlled upload → vault → offer → human message → purchase/refund behavior. A real fan identity and concrete authorised transaction are required before any live send/payment test.
- Verify combined earnings coverage/freshness, reconnection, account proxy isolation and restart/recovery. Hop vendor integration has not been audited here.
- Full staff invitations/assignments/reviews, shifts/payroll, dynamic packs and wider legacy automations remain separately scoped in the delivery plan.

## Evidence and recovery

SSM evidence:
- `a10e626f-41b8-492e-88bc-031b5f30d37b`: final-image acceptance succeeded, 160 + 80.
- `d3efd502-5103-4626-9642-da7b21ed5eee`: deployment succeeded, preserving schema/environment/volumes/outgoing policy state.
- `6fe62eca-b07c-494a-bbae-3a89c547d1fa`: pre-release live baseline.
- `1cd32551-437f-4193-8bbf-295d975024b5`: post-release counts and exact live image IDs, read-only DB transaction enforced.
- `e600a354-843b-4561-92de-fb8d482cef5f`: bounded native GET diagnosis; HTTP 503 before chats.
- `adb1e6fa-93d6-43ea-95da-d9373d598ee0`: one bounded identity GET classified the error response; Cloudflare header, no checked challenge/proxy-error indicators.

AWS profile `tsocial`, region `ap-southeast-2`, instance `i-09a9dd3bd749643a5`. Laptop Desktop Commander device `743b5427-502c-4ac7-9064-47b724f961f7`. Access was restored and used successfully; the earlier sign-in and laptop-offline blocks are resolved.

Server evidence: `/var/backups/twin-crm-core-20261021/71532fe06132c2f641eefe67c756b5defa585550/`.
Compose: `/etc/twin-onlyfans/media-agency-release-compose.json`.
Successful deployment removes the temporary image-retention container.

Windows owned worktree: `C:/Users/conta/Documents/ChatGPT/TWIN CLONE CREATION/twin-onlyfans-readonly-inbox-20261021`. Its last inspected local HEAD was `3909945`; fetch/fast-forward before further work. Correct fork remote: `twin-onlyfans`; `origin` points upstream.

Read-only inspected diagnostics:
- `C:/Users/conta/AppData/Local/Temp/twin-readonly-live-stats-ro.json`
- `C:/Users/conta/AppData/Local/Temp/twin-readonly-bounded-get-diagnosis.json`
- `C:/Users/conta/AppData/Local/Temp/twin-provider-error-kind.json`

No workflow permissions, IAM policies, tokens, proxy routing or fan-messaging settings were expanded to complete these checks.

## Continued work: provider cooldown and independent CI acceptance

PR13 https://github.com/jesse98/twin-onlyfans-backend/pull/13 is merged at `9febe8376ccbc1d27940ea587771aeacf488c44e`. Final code source is `283aa9f2049c875869e3d832f0d0c7c2326502db`. **This change is not deployed yet.** The last confirmed production image remains the PR12 image listed above.

Confirmed code defect: when a transient provider check failed, the loader's one-minute retry branch set `check_due=False` for an unexpired saved lease, then returned the account for normal inbox work. That allowed fresh work to evade the intended cooldown. The loader now returns no work during the cooldown, cached import-only readers reject unhealthy connections at claim/commit, and an account becoming unavailable after cohort loading no longer aborts the other accounts' poll requests. Saved activation intent, leases, policy allowances, revisions and fixed network routing are preserved.

The existing `scripts/run-service-tests.py` now runs four native PostgreSQL acceptance modules on GitHub-hosted CI using an owned temporary cluster. It disables TCP, uses a private Unix socket, verifies the data directory and listener setting before testing, and stops/removes only that cluster. Existing explicit local `--postgres` behavior is preserved. No workflow permissions or production credentials were added.

CI run `39611651391` passed both jobs on the exact final source. Python job `119261764102` ran 58 PostgreSQL cases: renewal 11, connections 28, read-only inbox 15, worker entry 4. These include cooldown enforcement, cached-reader fencing, automatic recovery without activation, and creator isolation. Existing script/unit checks passed too. Forty other PostgreSQL modules remain outside this critical CI smoke selection; this is not a full-suite claim.

Deployment status and blocker:
- Windows Desktop Commander went offline again, last seen about 27 minutes before the final inventory check. AWS access had worked immediately before it went offline; another AWS sign-in has not been requested or shown necessary.
- The next detailed 503 diagnostic file write, call `4b39aaf1-f2d7-48c8-a395-a0c93daab926`, definitively failed with device timeout. No new provider request was submitted for that diagnostic. Inspect file existence before reuse.
- The offline device prevented the additional checks for a configured proxy, bounded error-code/label indicators and Retry-After. Those findings remain unknown; do not invent a cause for the Cloudflare 503.
- The private sign-in service closes its browser after capture. Native reads use the saved capture and fixed route; no alternate browser/proxy/session has been substituted or verification bypassed.
- Existing GitHub workflows provide CI/build artifacts but no native live-deployment path. Do not repurpose workflow tokens or expand their permissions.

Prepared local work, reproducible from the committed source:
- `/workspace/scratch/6b5196797599/crm-retry/` contains the current source snapshot and `build-acceptance.py` test-only host harness.
- `/tmp/twin-provider-backoff-test.json` is the prepared SSM build/acceptance payload. It embeds only the seven changed source/test-runner files, starts from the exact PR12 image, verifies copied source hashes, retains the immutable candidate image, runs production-Python unit checks and an isolated database acceptance subset, and records candidate metadata. It has not been copied to the offline laptop or executed.
- Exact production-image checks and a guarded release still need to run after the remote connection is restored. Use the existing deployment helper hash, release lock, active-media/cleanup gate, and preserved environment/mount/schema/outgoing-policy checks already recorded here.
- No new image was built or live service changed in this continuation. No live fan send, upload, model call or purchase/refund was initiated. Inbox acceptance remains blocked, with the last observed provider response HTTP 503.
