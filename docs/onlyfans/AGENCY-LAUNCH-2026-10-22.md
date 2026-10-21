# Agency launch release — 22 October 2026 (Brisbane)

This supersedes deployment/status claims in the earlier agency launch, INBOX-SYNC-2026-10-22 and CRM-CORE-ACCEPTANCE-2026-10-21 checkpoints. Wider unfinished scope in DELIVERY-PLAN-2026-10-19 remains open.

## Released behavior
- Shared connection states distinguish temporary provider retry, automatic renewal checking, authentication rejection, explicit pause and unavailable metadata. A renewable verification deadline alone is not presented as definite logout.
- Creator/operator connection screens show recorded import pages, completed/remaining histories, pending/conflicting messages, recovery requirement and last complete sync. No completion is inferred from page counts.
- Visible account status refreshes in the background without resetting typed connection fields; identity, cancellation, stale-response and lost-ack behavior remain fenced.
- Native GET ?progress=1 adds this redacted snapshot from existing canonical scan/recovery/ingress/runtime owners. Ordinary GET retains its exact previous response shape. Explicit pause/failure remains visible after a local deadline.
- Owner Agency access supports password-based staff login creation, existing membership access changes, revoke and restore. Internal-owner authentication, trusted form origin, exact agency/user/email/expected-state filters and existing fixed agency policies remain enforced. Failed membership creation removes only the newly created auth user.
- Staff share agency-level creator assignments. Creator assignment stays in the existing creator-management workflow. There is no new per-member creator assignment, agency-owner delegation, email invitation flow, role/schema, provider route, retry loop, renewal owner or send policy.
- Existing main 79f384 message-loading changes are preserved. The overview browser fixture now models the existing server action's layout revalidation; production creator-switching behavior was not changed by that fixture correction.

## Backend release and validation
- PR15: https://github.com/jesse98/twin-onlyfans-backend/pull/15
- Source 39dec86cbbc68c41dca1fdc36c465b3c722ae231; merge 4e16cde43bec868ef941992ef834512ebbf7995b.
- API and worker image sha256:9dd1223db42821472e2ee3643e8d20332406a728e0557096b178b123be12c335.
- Exact production-Python acceptance: 161 unit +82 isolated PostgreSQL cases passed, SSM 802ff667-79cc-4057-82de-2fd52b8ffc54. CI 39629222568 independently passed renewal11, connections30, enrollment17, inbox20 and worker-entry4 PG cases.
- Guarded deployment b8f70a2a-7903-43fb-8964-c19a53e50be8 passed. Schema0088, environments, volumes and outgoing settings were preserved; active-media/cleanup gate passed. Owned image-retention container removed.
- Authenticated HTTP f398fee1-9983-42e6-b2a4-4e9747bd726a and refresh10218fc8-9122-4456-b51f-e4e50c49cda9 verified ordinary/opted response shapes and both running images.
- The previous image369c750e97f5e6c8d36cdb3faa379fa80c8f6bd85bd46c4eb33fd9b095d87101 is rollback evidence, not the running release.

## Dashboard release
- PR242: https://github.com/jesse98/twin-dashboard/pull/242
- Tested source9c9417a4b2ea2dda84431c39e1f6c706bff9dbda; merge/running GIT_SHA b9f8c81e46fa8abdf1b0eb4e58618aa7cd8bf371.
- Running next-app image sha256:538f86e5561bc2fcf13ab5110f80b3d4a518066828cc572ad70fc91a1d688234, independently confirmed by SSM b43d8349-3406-46ab-adaf-8d99710a94a2.
- Full current-source CI39632497264 passed both build/type/browser/behavior and PostgreSQL identity/ownership jobs. Creator sign-in39632497261, workspace39632497263 and LYNQ visual checks also passed.
- Workflow39634314864 deployed the exact merge successfully (SSM9552ff33-2f74-4ac7-ad09-a7d11496238f). Required creator-uploads and separate test jobs passed; live agency earnings/private-photo and normal Moxy signup/75MiB upload-boundary/exact-cleanup checks passed.
- Independent normal-owner-login verification e2ba7aae-1e3a-4f85-a656-d2ed08aae65c passed: owner team page200 with three existing member rows, authenticated progress200/no-store, anonymous request refused. Session tokens/passwords were not printed or saved.
- The workflow's final catalogue gate is FAILED, not green: all30 creators and1313 videos checked;1309 passed,4 invalid originals failed at probe. Current audit SSM e76f50aa-e3d3-4a62-b1c4-f85c444cfe9a. Exact failure creator/video IDs, stage and reason match prior deployment39629526793 on79f384; this is the recorded Briana damaged-source baseline. No gate was removed or disabled, no media was deleted, and the deployed healthy owner/connection feature was not rolled back for that unchanged baseline.
- Failed video IDs:939f5799-692e-4feb-ab88-ec75e0fbcf56, a55c1149-7f81-43e2-9b4a-9d523dfe35fb, b5a18847-7c8a-43ee-8200-86b1c957436e, e0ea6098-1628-4365-b276-d559f3a1cbbd. Creator0328e7b3-a1e5-465a-9a30-c9cf6519cba1. All stage=probe, reason=invalid.
- The workflow's later public Ava check was skipped after this catalogue failure. A separate normal public HTTP check passed for /ava and /ava/, with the expected revision and no login redirect; this additional evidence does not make the failed workflow green.

## Live limitation
Final authenticated dashboard and native reads: of-176774528 status verified, provider_connection_check_retry, outgoing disabled; discovery running, pages0, complete histories0, remaining histories0, pending/conflicting messages0, recovery required, lastCompleteSyncAt=null. Verification deadline2026-10-21T18:30:34.681362Z; last verified2026-10-21T12:30:34.683759Z. This is a provider retry with no complete import, not evidence that the whole CRM works live.
The selected account of-176774528 has no proven successful native inbox import. Earlier same-route OnlyFans GET returned HTTP503 with planned-maintenance HTML; no global outage is inferred and no provider recovery is claimed without a successful subsequent check.
Temporary provider retry does not prove the saved login has been revoked. The renewable verification deadline is extended only after successful identity verification; unlimited connectivity is not guaranteed.
No fan message, model call, purchase, refund, invitation email or real staff grant was initiated. The existing dashboard workflow ran isolated normal-signup/upload probes with exact cleanup.

## Authorization and continuation
The earlier automatic approval-review rejection for GitHub/AWS source transmission was resolved by the user's explicit “Yes I do” approval for publishing, testing and deployment to these repositories and the existing AWS host.
Use jesse98/twin-dashboard and jesse98/twin-onlyfans-backend; never push the dashboard's inherited ronan-com origin or backend's BlackPhoenixSlo origin. Correct dashboard remote is the explicit jesse98 URL; backend remote twin-onlyfans.
Owned Windows worktrees: C:/Users/conta/codex-work/agency-launch-dashboard-20261022 and agency-launch-backend-20261022.
AWS profile tsocial, ap-southeast-2, instance i-09a9dd3bd749643a5. Evidence: /var/backups/twin-crm-core-20261021/39dec86cbbc68c41dca1fdc36c465b3c722ae231/.
Release lock /var/lock/twin-onlyfans-native-release.lock and helper hash ff58db782534fbf86f5e08cdd411b09571b2b2410811e075297c148e0d662d41 retained. Do not replay completed release payloads.
Live inbox/send/media/payment acceptance, complete audience/campaign work, protected playback, deeper staff permissions and restart/backup recovery remain unfinished gates. This release does not claim the entire Hybrid CRM is complete.
