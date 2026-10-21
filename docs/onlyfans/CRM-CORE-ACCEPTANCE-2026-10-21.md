# CRM core acceptance checkpoint — 21 October 2026

This is the current OnlyFans CRM checkpoint. The entire CRM is not complete. Read alongside DELIVERY-PLAN-2026-10-19.md; earlier checkpoints and synthetic tests do not prove live end-to-end acceptance.

## Current release: deployed and confirmed

- Current backend PR13: https://github.com/jesse98/twin-onlyfans-backend/pull/13; builds on deployed PR12.
- Integrated branch: `codex/onlyfans-agency-access-20261020`; PR13 merged at `9febe8376ccbc1d27940ea587771aeacf488c44e`.
- Running API and worker source: `283aa9f2049c875869e3d832f0d0c7c2326502db`.
- Running image on both containers: `sha256:d9155c3685248da6988812ecbe025ad5576ea7d9eb4d4d0770f810ad39cc0534`.
- Current deployment command `1ab25055-723a-4b0d-9373-a0ceed3c1c49` **succeeded**. Independent post-release inspection confirms the exact image on both live containers. Do not submit the release again.
- Previous PR12 source `71532fe06132c2f641eefe67c756b5defa585550`, image `sha256:3ee6c93965b1f73deef84c37642bfe4aa91a10281cf35c9d9d5ac2165d2a058e`, and successful deployment `d3efd502-5103-4626-9642-da7b21ed5eee` are historical rollback evidence.
- Schema remains `0088_native_vault_media`. Deployment confirmed preserved environment, persistent volumes and outgoing policy state, and no interrupted active media work or upload cleanup.
- The release includes PR11's renewable verified connection leases. It does not overwrite that fix with an earlier inbox candidate.
- No live fan send, model call, purchase, refund or upload was initiated for this acceptance.

## Implemented and tested

Verified connected native accounts can now load and periodically refresh canonical Fan/Chat/Message history while outgoing messages remain disabled and no model credential is selected.

The import-only worker skips model, planning, campaign, receipt-repair and delivery phases. Every read claim and page commit checks the current tenant binding, verified connection revision and expiry. Pausing, replacing or activating the connection while a GET is in flight prevents the stale page from committing. History queued before activation remains historical after activation and cannot create a reply.

Existing active-account behavior, bounded creator-cohort paging, send authority, financial evidence and ownership safeguards remain in place. This adds no schema, duplicate CRM store or dashboard layout change.

The current exact production image passed **161 unit checks and 30 isolated PostgreSQL checks**. Its exact-source GitHub CI independently passed 58 PostgreSQL checks. PR12 previously passed 160 unit checks and 80 isolated PostgreSQL checks. Both GitHub CI jobs passed. An unchanged frontend font-resolver failure passed on one retry. The prior broad candidate passed 120 PostgreSQL cases and exposed two fault-injection call-contract failures; preserving the existing normal intake call fixed them, and both cases passed in the final run. Earlier candidates `8710eee` and `3909945` must not be deployed.

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

Earlier bounded diagnostic reads through the saved account connection returned HTTP **503**, `text/html`, 21,853 bytes from the `/api2/v2/users/me` identity endpoint, before the chats request could run. The second read identified the Cloudflare server header. Neither a challenge header nor the checked challenge/proxy-error markup indicators were present.

After PR13 deployment, one bounded identity GET through the worker's unchanged saved route again returned HTTP 503 and 21,853 bytes. This account has **no proxy configured**. There was no numeric Retry-After, no extracted three/four-digit error code, none of the bounded whitelisted error labels, and none of the checked challenge-markup indicators. This does not establish the cause. The earlier attempt from the API process was correctly refused before network I/O; its result was not a provider response. No proxy, cookies, fingerprint, signing rules or account route was changed.

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

Additional PR13 evidence:
- `c7bc0f2e-9881-4ff0-b717-edc9aae34bd6`: exact immutable production-image acceptance, 161 unit + 30 isolated PostgreSQL checks passed.
- `1ab25055-723a-4b0d-9373-a0ceed3c1c49`: guarded deployment succeeded; environment, mounts, schema and outgoing policy state preserved; owned image-retention container removed.
- `e75d0d21-323c-4208-a33c-20f8bfdf37d6`: independent read-only post-release counts and exact API/worker image identities.
- `b6b2d0c7-adb2-442c-ba67-7fc4a5a23195`: post-release bounded worker identity GET still HTTP 503; no proxy configured, no numeric Retry-After or checked error/challenge indicators.
- `c771ba0b-5424-415b-95e2-53930f13fd3b`: diagnostic from API process refused before network I/O. Not evidence of a provider request.

AWS profile `tsocial`, region `ap-southeast-2`, instance `i-09a9dd3bd749643a5`. Laptop Desktop Commander device `743b5427-502c-4ac7-9064-47b724f961f7`. Access was restored and used successfully; the earlier sign-in and laptop-offline blocks are resolved.

Current server evidence: `/var/backups/twin-crm-core-20261021/283aa9f2049c875869e3d832f0d0c7c2326502db/`.
Compose: `/etc/twin-onlyfans/media-agency-release-compose.json`.
Successful deployment removes the temporary image-retention container.

Windows owned worktree: `C:/Users/conta/Documents/ChatGPT/TWIN CLONE CREATION/twin-onlyfans-readonly-inbox-20261021`. Its last inspected local HEAD was `3909945`; fetch/fast-forward before further work. Correct fork remote: `twin-onlyfans`; `origin` points upstream.

Read-only inspected diagnostics:
- `C:/Users/conta/AppData/Local/Temp/twin-readonly-live-stats-ro.json`
- `C:/Users/conta/AppData/Local/Temp/twin-readonly-bounded-get-diagnosis.json`
- `C:/Users/conta/AppData/Local/Temp/twin-provider-error-kind.json`

No workflow permissions, IAM policies, tokens, proxy routing or fan-messaging settings were expanded to complete these checks.

## Continued work: provider cooldown and independent CI acceptance

PR13 https://github.com/jesse98/twin-onlyfans-backend/pull/13 is merged at `9febe8376ccbc1d27940ea587771aeacf488c44e`. Final code source is `283aa9f2049c875869e3d832f0d0c7c2326502db`. **This change is deployed and independently confirmed.** The running image and deployment evidence are listed above.

Confirmed code defect: when a transient provider check failed, the loader's one-minute retry branch set `check_due=False` for an unexpired saved lease, then returned the account for normal inbox work. That allowed fresh work to evade the intended cooldown. The loader now returns no work during the cooldown, cached import-only readers reject unhealthy connections at claim/commit, and an account becoming unavailable after cohort loading no longer aborts the other accounts' poll requests. Saved activation intent, leases, policy allowances, revisions and fixed network routing are preserved.

The existing `scripts/run-service-tests.py` now runs four native PostgreSQL acceptance modules on GitHub-hosted CI using an owned temporary cluster. It disables TCP, uses a private Unix socket, verifies the data directory and listener setting before testing, and stops/removes only that cluster. Existing explicit local `--postgres` behavior is preserved. No workflow permissions or production credentials were added.

CI run `39611651391` passed both jobs on the exact final source. Python job `119261764102` ran 58 PostgreSQL cases: renewal 11, connections 28, read-only inbox 15, worker entry 4. These include cooldown enforcement, cached-reader fencing, automatic recovery without activation, and creator isolation. Existing script/unit checks passed too. Forty other PostgreSQL modules remain outside this critical CI smoke selection; this is not a full-suite claim.

Deployment continuation:
- The laptop returned online. Existing AWS access worked; no additional sign-in or privilege changes were needed.
- The prepared seven-file build payload matched the reviewed local source. It built from the exact PR12 image, verified copied source hashes, ran 161 unit and 30 isolated PostgreSQL checks on production Python 3.12, and passed.
- The guarded release used the existing helper hash, release lock, active-media/cleanup gate, immutable image and environment/mount/schema/outgoing-policy preservation checks. It succeeded and removed only its owned image-retention container.
- The selected account remains verified with saved expiry 18:30:34 UTC and `provider_connection_check_retry`; outgoing runtime and policy are false. Post-release counts remain 30 fans / 0 chats / 0 messages / 40 existing transactions, with no model/reply/text work and no completed inbox scan.
- The actual worker identity GET still returns HTTP 503. No proxy is configured on this account; Hop vendor provisioning and account-route setup remain unaudited and incomplete. Do not rotate or substitute routes to evade a provider block.
- The private sign-in service closes its browser after capture. No alternative browser/session or provider-verification bypass has been substituted.
- No live fan send, upload, model call or purchase/refund was initiated. The entire CRM and live inbox acceptance remain incomplete.

Prepared payloads used in this continuation are in Windows Temp: `twin-provider-backoff-test.json`, `twin-provider-backoff-release.json`, and `twin-provider-worker-route-probe-pr13.json`. Corresponding SSM evidence is above; do not blindly replay them.
