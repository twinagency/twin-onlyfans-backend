# Agency launch release — 22 October 2026

## Current native release
This supersedes the native deployment state in INBOX-SYNC-2026-10-22.md and CRM-CORE-ACCEPTANCE-2026-10-21.md. Their wider unfinished scope remains open.
- Backend PR15: https://github.com/jesse98/twin-onlyfans-backend/pull/15
- Source 39dec86cbbc68c41dca1fdc36c465b3c722ae231; merge 4e16cde43bec868ef941992ef834512ebbf7995b.
- API and worker: sha256:9dd1223db42821472e2ee3643e8d20332406a728e0557096b178b123be12c335.
- Exact production-Python acceptance: 161 unit + 82 isolated PostgreSQL cases passed (SSM 802ff667-79cc-4057-82de-2fd52b8ffc54).
- GitHub CI 39629222568 passed both Python and dashboard jobs; the Python job independently ran renewal 11, connections 30, enrollment 17, inbox 20 and worker entry 4 database cases.
- Guarded deployment b8f70a2a-7903-43fb-8964-c19a53e50be8 succeeded with schema 0088, environment, volumes and outgoing settings preserved. Active upload/cleanup gate passed. The owned image-retention container was removed.
- The prior image 369c750e97f5e6c8d36cdb3faa379fa80c8f6bd85bd46c4eb33fd9b095d87101 is rollback evidence, not the current image.

## Native behavior
GET /onlyfans/connection/v1/accounts/{account}?progress=1 adds a redacted snapshot from the existing scan, recovery, ingress and runtime owners. Ordinary GET retains its old exact shape. No new import store, worker, schema, credential renewal, proxy/provider route, signing rule, send policy or retry loop.
Explicit paused/failed states remain visible after an elapsed local verification deadline.
Source tests prove scope/redaction, no progress mutation, no completion inferred from scan pages, and retention of pause/authentication failure.

## Live observation
Authenticated HTTP verification f398fee1-9983-42e6-b2a4-4e9747bd726a confirms the legacy shape and actual opt-in endpoint, plus both running image IDs.
Selected account of-176774528: verified, provider_connection_check_retry, outgoing disabled.
Progress: discovery running, pages 0, complete histories 0, remaining histories 0, pending/conflicting messages 0, recovery required, no completed sync.
This is not a successful import. Earlier same-route OnlyFans GET returned HTTP503 planned maintenance; no global outage or provider recovery is claimed.
API container port is 8787, mapped to host loopback 8788. An initial diagnostic incorrectly used the host port inside the container and was corrected after inspecting Docker's actual mapping.

## Paired dashboard in progress
PR242: https://github.com/jesse98/twin-dashboard/pull/242
Candidate head 8b40af7121f0a637063f4400a8962ca56ec8da9b includes current main 79f3842967d00727752b1bcfa3bde902f68f4af3, preserving the separate message-loading work. New current-source CI 39631054030 is running. Do not claim the new dashboard is live yet.
The candidate distinguishes temporary provider retry from login rejection, shows recorded progress, and completes the existing owner team screen with login creation, existing access changes, revoke/restore and stale-form guards.
Staff share agency-level creator assignments. There is no per-member creator assignment, agency-owner delegation, new email invitation flow or new role/schema. Existing fixed legacy policies remain intact.
Local type, real Chromium desktop/mobile creator-connection, owner team route/rendered-form, owner/admin/agency/connection and preview/channel tests passed. Original candidate source 85c3803 also passed creator sign-in and workspace CI, with full CI nearing completion before main integration.

## Authorization and continuation
The earlier automatic approval rejection for GitHub/AWS source transmission was resolved by the user's explicit “Yes I do” approval to publishing, testing and deployment to the existing repositories and server.
GitHub targets: jesse98/twin-dashboard and jesse98/twin-onlyfans-backend. Do not push the dashboard's inherited origin remote (ronan-com) or the backend's upstream origin (BlackPhoenixSlo). Use the explicit jesse98 dashboard URL and backend twin-onlyfans remote.
Windows worktrees: C:/Users/conta/codex-work/agency-launch-dashboard-20261022 and agency-launch-backend-20261022.
Desktop Commander device 743b5427-502c-4ac7-9064-47b724f961f7; AWS profile tsocial, ap-southeast-2, instance i-09a9dd3bd749643a5.
Server evidence: /var/backups/twin-crm-core-20261021/39dec86cbbc68c41dca1fdc36c465b3c722ae231/.
Release lock /var/lock/twin-onlyfans-native-release.lock and helper hash ff58db782534fbf86f5e08cdd411b09571b2b2410811e075297c148e0d662d41 retained. Do not replay the completed backend release.
Finish dashboard CI, merge preserving current main, observe the existing deployment workflow, then run authenticated owner team-page and connection-progress verification. The prepared read-only feature verification is agency-launch-dashboard-live-verify.json in Windows Temp; it uses the existing normal master login and does not change memberships.

No fan send, upload, model call, purchase, refund, invitation email or real staff grant was initiated. Wider live inbox/send/media/payment acceptance, complete audience/campaign work, protected playback and deeper staff/recovery scope remain open.
