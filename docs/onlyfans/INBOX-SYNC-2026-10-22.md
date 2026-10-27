# Automatic connected inbox sync — 22 October 2026 (Brisbane)

Current release: [AGENCY-LAUNCH-2026-10-22.md](AGENCY-LAUNCH-2026-10-22.md) supersedes the deployed source and live status below; the wider unfinished scope remains open.

This supersedes the release and inbox-status sections of CRM-CORE-ACCEPTANCE-2026-10-21.md. The wider media/payment/CRM scope in that checkpoint and DELIVERY-PLAN-2026-10-19.md remains open.

## User priority
Prioritise the connected account's live inbox/messages and history import; new accounts must sync automatically when connected. This task did not select a real fan recipient or a paid test transaction. No live fan messages, purchases or refunds were initiated.

## Deployed source
- PR14: https://github.com/jesse98/twin-onlyfans-backend/pull/14
- Integrated branch: codex/onlyfans-agency-access-20261020
- Merge: 00c23319015a638b793125c3cb65ff5c067ca3b0
- Runtime source: 25eddd6097a5ff18f7633c5576ac4d6dbf65685c
- API and worker image: sha256:369c750e97f5e6c8d36cdb3faa379fa80c8f6bd85bd46c4eb33fd9b095d87101
- Deployment dc23593b-1f8b-476f-a9c6-04e9999c37ea succeeded. Do not redeploy the same payload.
- Independent read-only verification e182dbf7-7585-4ff1-9985-3552e4bc83fe confirmed both exact images running.
- Schema 0088_native_vault_media, environment, mounts and outgoing policies preserved. Release lock, active-media/cleanup gate and rollback helper retained.
- Previous PR13 image d9155c3685248da6988812ecbe025ad5576ea7d9eb4d4d0770f810ad39cc0534 is rollback evidence.

## Behaviour
Existing PR12/PR13 code begins a canonical history scan on connection save, verifies pending accounts, imports while outgoing is disabled, and continues polling. It already handles all connected accounts in bounded rotating cohorts.

The new fix addresses two first-connection failure cases:
1. A transient NativeProtocolError during a pending connection's first identity verification no longer permanently fails that connection. The same encrypted capture and original expiry are retained, with the existing one-minute retry pacing.
2. A temporary enrollment identity failure no longer destroys the capture immediately. The existing pending enrollment is retried after its durable cooldown; waiting rows are skipped so other creators proceed. The encrypted normalized capture remains frozen across retries and worker restarts.

Authentication rejection, expired unverified captures, account/creator/tenant boundaries, pause/reconnect revisions, stale enrollment generations and outgoing activation remain enforced. No schema, route, proxy, cookie, signing-rule, fingerprint or frontend changes. No provider-verification bypass.

## Validation
- Exact-source GitHub CI 39618804195 passed both Python and dashboard jobs.
- CI database coverage: renewal 11, connections 28, creator enrollments 17, read-only inbox 20, worker entry 4 = 80 cases.
- Exact production Python 3.12 image: 161 unit checks plus 80 isolated PostgreSQL cases passed.
- Production-image acceptance SSM: 86a13dce-e249-4f46-8421-be38a04ccc86.
- Tests prove first verification failure -> paced recovery -> automatic history import -> later-message refresh without outgoing activation/model work; enrollment retry survives restart and does not block another creator.
- Rejected authentication, expiry, stale generations, pause/replacement and existing account boundaries also pass.
- Tests used an owned isolated database with no production credentials or provider access.

## Live blocker: provider explicitly returns planned maintenance
The saved account is of-176774528 (Jesse Creator Test).
Normal identity GET through the unchanged worker account route returns HTTP 503, 21,853 bytes, text/html, Cloudflare server header.
Read-only diagnosis 41e63d2b-a68f-4eec-a5c3-c67786557e03 extracted visible response text:
"OnlyFans is currently offline for planned maintenance. We will be up and running again soon."

This establishes the response on this account's current route; it does not establish global availability for every OnlyFans client. No CAPTCHA/challenge marker was observed. No proxy is configured on this account. Do not rotate routes or bypass verification. No extra sign-in is currently justified by this evidence.

Post-deployment read-only state:
- Saved connection verified, revision 2; provider_connection_check_retry.
- Last attempted verification observed: 21 October 14:47:43 UTC / 22 October 00:47:43 Brisbane.
- Last successful verification remains 21 October 12:30:34 UTC; original renewable lease expires 18:30:34 UTC.
- 30 existing fans / 0 chats / 0 messages / 40 existing transactions.
- Inbox scan running, 0 completed pages; no completed account sync.
- Outgoing runtime and policy false.
- Model calls / reply work / text intents all zero.

The worker continues normal bounded verification retries. Import should resume automatically once matching-account verification succeeds. Never report a successful live import until actual canonical chat/message commits and completed scan are observed.

## Next step
Check provider recovery and the existing scan using read-only diagnostics. If identity succeeds, observe full history discovery/recovery/drain and subsequent polling, then verify the operator inbox and human takeover. Counts alone or one page do not establish full acceptance. Live sending/payment tests still need a concrete selected recipient/transaction.

## Access and evidence
- AWS profile tsocial; region ap-southeast-2; instance i-09a9dd3bd749643a5.
- Desktop Commander laptop 743b5427-502c-4ac7-9064-47b724f961f7.
- Working tree C:/Users/conta/codex-work/onlyfans-sync-20261022. Fork remote twin-onlyfans; origin is the upstream project.
- Server evidence /var/backups/twin-crm-core-20261021/25eddd6097a5ff18f7633c5576ac4d6dbf65685c/
- Compose /etc/twin-onlyfans/media-agency-release-compose.json
- Inspected read-only state payload C:/Users/conta/AppData/Local/Temp/twin-sync-final-live-state-20261022.json
- Test/release payloads twin-sync-first-connect-test-20261022.json and twin-sync-first-connect-release-20261022.json in Windows Temp. Do not blindly replay.
