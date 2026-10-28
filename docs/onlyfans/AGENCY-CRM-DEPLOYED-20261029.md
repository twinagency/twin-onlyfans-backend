# Agency CRM deployed repair and acceptance boundary

Recorded 28 October 2026 UTC / 29 October AEST. This is the current release record and supersedes the candidate-only status in AGENCY-CRM-LAUNCH-20261028.md. Repair code and the additive staff schema are deployed. Commercial launch acceptance is incomplete. No new creator/staff cohort is certified safe to launch, and Infloww parity is not claimed.

## Exact running release

Post-deployment SSM verification completed successfully: command `0cb45f7a-64b0-4e68-8942-5f10ebe9dd57`. Four containers running, zero restarts.

| Component | Application source | Immutable running image |
| --- | --- | --- |
| Dashboard | ce99d8acdc0a09b6492606c1c8ba417b047d6bed | sha256:3f9bb9c48025a32b94cedaff6ff31b06c2dfac9a18367b3a210688aa0b88d676 |
| API | 528160762fdc45f62b73dbaebf39de9af5a4c59d | sha256:3e6e267ddef3ed8b764d43069ae54765f90087bbce74dfb68324b5f3a1cdf191 |
| Inbox/manual worker | 1dcb9846fe1ebee6dc4fe389fa6038ce1313774f | sha256:e45c4d6ff2285fa29b72f93fb67b88850d1a71b5b77a33c69e6d77ee3af44438 |
| FASTT supervisor/sender | f234c641a0afd68b3314ad428284fcc24c96f1c2 | sha256:80341b333fb171478d555a5d729fb6375420afaf5d0a38b64ad4cacc467bf756 |
| Backend schema | 0098_ledger_exclusion | unchanged |
| Dashboard staff schema | 20261028125157_onlyfans_scoped_chatter | additive, applied and verified |

Review heads: dashboard **bff99537d8af2559704a7286af7bb1a029eecc18**; backend **ea5a17ca7079e1d412a02138ff9cb6af4cd71e8d**. Compared with the running application sources, only the authenticated browser assertion file and its cross-repository test pin differ. There are no runtime file differences. Do not describe these test-only heads as the running image sources.

Review PRs: [dashboard #312](https://github.com/twinagency/twin-dashboard/pull/312), [backend #40](https://github.com/twinagency/twin-onlyfans-backend/pull/40).

## Current release was established before replacement

Audit snapshots were treated as historical. Activation/handover records were read. Actual image/source/schema identities and local state were inspected. Dirty laptop/server checkouts were preserved; isolated clean checkouts and review branches were used. Multiple newer production releases occurred during the repair. A source/image guard stopped an earlier cutover before mutation when production advanced. Dashboard release 92cf24794018df8dd212378a4c4f8be2fd58d7d1 and backend worker/API 1dcb9846fe1ebee6dc4fe389fa6038ce1313774f were reviewed and merged before deployment.

The independently deployed PR #38 continuation fix is retained in worker ancestry. Its continuation, deferred retry, replacement-binding and PostgreSQL regression coverage were retained. This task did not replace worker or sender with the older audit application build. Later profile, vault, ownership, provider-evidence worker isolation, uncertain-delivery and representation-only history repair work is preserved.

Original authenticated browser failure was reproduced at /api/onlyfans-launch/abc/accounts/of-176774528/readiness, with of-launch.v1 / unavailable and generic evidence error, including failed Retry. Trace followed dashboard authentication and authorized account/agency scope through the backend endpoint and strict envelope/account parsing to database timing. Admin login itself succeeded. Final browser checks exercise the same BFF/UI path and now show current evidence.

## Implemented fixes and demonstrated causes

| Area | Root cause / finding | Deployed behavior |
| --- | --- | --- |
| Readiness evidence unavailable | Authenticated request reached the BFF/backend, but full source-validating memory receipt join took 18.815 seconds against an 8-second dashboard / 10-second database boundary. A later cold exact stored-message COUNT also exceeded the database deadline. | Bounded keyed source-validating sample (50); full processing remains unknown. Exact stored count gets a 1.5-second SAVEPOINT-local deadline. Only PostgreSQL cancellation 57014 becomes an explicitly unavailable count; unrelated errors remain errors. Original timeout restored. No zero, empty successful evidence or green full coverage is fabricated. |
| Read-side contention | Readiness/ledger scope acquisition used runtime locks also needed by mutation/admission. | Explicit read-only REPEATABLE READ snapshot, ownership/binding fence, no runtime/catalog locks only in that snapshot. Commands and credential mutation paths keep their default locks and authority. PostgreSQL isolation/rebind/cancellation tests cover this boundary. |
| Error handling | Generic BFF error hid transport, schema and account-identity failures. | Safe distinct errors with evidence reference, last-success and retry/check-in-progress states; bounded transport and JSON/account/schema validation. |
| Draft loss | Composer state/remount resets and late acknowledgement could discard another conversation or newer draft. | Document-memory key includes authenticated principal/agency, account and conversation. Text and selected catalog content survive chat/SPA remount. Clear only captured revision after non-simulated acknowledgement. Eight-hour TTL; reload/logout clears. No disk persistence. |
| Acting creator resets | Navigation omitted account context; Launch selector did not update URL. | Authorized account query retained in sidebar/section navigation. Launch selection updates query and removes stale creator query. Invalid requested account does not silently choose another. Explicit replying-as identity in composer/controls. |
| Search | Loaded-row browser filtering; request-ID-length cursor rejection. | Server account/binding scoped fan name, username and ID search before pagination, copied-@ normalization, escaped wildcards and query/filter-bound cursor up to 512 chars. Message content is explicitly excluded. Paused/review/delivery filters honestly say when they cover loaded results only. |
| Money | No canonical financial projection; purchase/asset-derived totals risk duplicate counting. | Canonical transactions counted once, separated by currency/payment status with gross/net and coverage time. Missing/stale/partial states remain visible. |
| Notes | Existing audited memory correction was outside inbox. | Reuse BMemoryPanel with selected authorized account/fan and existing reason/version/idempotency/permission workflow. No competing notes store. |
| Staffing | Manager/read-only could not express narrow messaging work. | Default-false explicit can_chat membership/invite capability; v1/v2 agency scope, assigned creator checks and backend action allowlist. Chatter can approved manual messaging, takeover/hand-back, audited notes and permitted media selection; cannot configure, manage credentials/team, bulk AI, campaigns or imports. Existing legitimate manager policy/access retained. |
| Purchase blocker counter | Counter used ingestion time while commerce uses occurrence relative to binding. | Counter now matches occurred_at guard. Historical unlinked purchases remain unlinked; ownership, unknown-send and duplicate protections remain. |
| Scan running / no lease | Page/lease/backoff lifecycle permits running between leased pages. | Explain waiting/backoff/lease state and actual retry reason. No forced reset, bypass or fabricated active lease. |
| Approval mismatch | Fingerprint mismatch lacked exact explanation. | Immutable audit-chain before/after differences, actor/time/completeness. No snapshot reapplication or automatic reapproval. |
| Daily health | Connection count was mistaken for operational readiness. | Distinct connection, history, payments and sending-readiness evidence; incomplete coverage and holds stay visible. |
| Bulk AI wording | Implied all/new fans automatically enabled. | Eligible existing conversations wording and explicit locked new-fans-off policy. |
| Audience/action read failure | Original single transient failures have no retained evidence proving their exact provider/network cause. | One bounded GET retry for network/502/503/504 only, honoring abort/backoff. Auth, persistent errors and writes are not hidden/retried. This is bounded recovery, not a claim of a proven original upstream cause. |
| Isolated login smoke | Test depended on unavailable external session store despite working live admin login. | Revocable local session HTTP fixture exercises real production authentication: redirect, wrong password, revoked token denial and outage fail-closed. No production bypass. |

## Live production findings, without clearing protections

- **Nicky history:** zero remaining quarantine observed. The newer independent release repaired all 141 representation-equivalent provider HTML records in two immutable audited commands. Raw payloads, event keys, binding/fan/message identities retained. Four records' ordered media IDs matched exact scoped retained vault_sends. This task preserved and regression-tested that recovery; it did not delete evidence, disable quarantine or narrow an account-wide hold.
- **Recovery is repeatable:** inspect an authorized account/fan, review its evidence/fingerprint, repair with exact fingerprint, stable command key, internal authenticated actor and reason; same-key replay is idempotent. Changed/substantive evidence refuses. Existing provider-evidence resolution runs only in isolated worker, never API. Unknown delivery remains unknown without positive proof.
- **Purchases:** Nicky has 1,732 unlinked historical paid PPV purchases across 944 fans. All 1,732 precede the current binding; zero current-binding/unexcluded purchases were found. No missing provider message ID was guessed and no purchase was marked linked. The historical unlinked rows alone are not the current-binding blocker.
- **Commerce hold scope:** Nicky still has an account-wide ledger freshness/reconciliation hold, so paid sends are held for every fan/media selection. This is not a claim that every historical fan owns every media item. Private per-purchase evidence was retained; no ownership inferred or hold narrowed.
- **Scan lifecycle:** a running scan actually advanced and reacquired a lease. Observation: last complete 20:20:10 UTC, updated 22:36:31, active lease until 22:37:31, offset 2580. Full-history scan separately idle, last complete 20:52:24, no lease, offset 2680. This demonstrated working page/lease lifecycle rather than an orphan needing reset.
- **Configuration:** personality revision 3 approval refers to configuration revision 2. Immutable revision 3 change at 2026-10-27T17:13:30.658221: fasttScope changed from null to {"fans":[],"mode":"all"}. Saved persona and facts matched approved content; this substantive effective scope change was preserved. Approval remains stale pending appropriate review. No old snapshot reapplied.
- **Activation preserved:** Nicky enabled with 3 saved automated conversations; Naomi enabled with 4. Alva, Katie and Maile remain disabled. Maile's saved fan flags do not override account pause. No new fan/creator/staff activated. The already-automated designated test conversation was temporarily taken over for the authorized manual acceptance and then returned to its prior automated mode.

## Final-source tests actually run

| Source | Verification | Result |
| --- | --- | --- |
| Dashboard ce99d8a | [CI 40782601514](https://github.com/twinagency/twin-dashboard/actions/runs/40782601514): real PostgreSQL creator identity/ownership; build/type; interface/controls/navigation; behavioral checks; production login routing/outage fixture | Both jobs passed, including final build and login smoke |
| Dashboard ce99d8a | Local TypeScript, 7 agency launch contracts, 17 launch controls, 24 B/native launch tests, retained video contract | Passed |
| Dashboard ce99d8a | [Immutable Docker image build 40782882878](https://github.com/twinagency/twin-dashboard/actions/runs/40782882878), artifact 12858440784 | Built; archive SHA/config/RootFS and source verified before deployment |
| Backend 5281607 | [Targeted final regression 40782878366](https://github.com/twinagency/twin-onlyfans-backend/actions/runs/40782878366/job/123148712525): 6 retry + 8 read-only/readiness/slow-count + 7 agency contracts | 21 passed; 17 unrelated cases deselected by explicit selection |
| Backend 5281607 + dashboard ce99d8a | First authenticated HTTP/PostgreSQL browser run | Failed fixture assertion expecting history warning when the deliberately unverified provider connection correctly had priority. No application failure inferred or assertion removed. |
| Review heads ea5a17c + bff9953, runtime files identical to deployed sources | Corrected strict connection-warning fixture, [authenticated HTTP browser run 40784283777](https://github.com/twinagency/twin-onlyfans-backend/actions/runs/40784283777) | Passed, 1 complete real HTTP/PostgreSQL/browser journey; strict connection-warning assertion retained, no external provider requests allowed |
| Production final images | Read-only API: Nicky readiness 5.442s, Alva 4.491s, unauthorized 401, unavailable account 404, schema 0098; four container/source/image checks | Passed; both accounts honestly not ready to enable |
| Production final dashboard | Authenticated admin browser: Nicky and Alva readiness evidence + last-success + null-count explanation; account switch URL and sidebar context; Nicky retained into Inbox; controlled fan @u318022380 search returned one matching row, canonical USD 6.00 cleared gross / USD 4.80 net with coverage time and commerce hold; existing audited memory panel opened | Passed |

Historical checks are not attributed to the final source: 142 PostgreSQL tests, broader sender PostgreSQL/SQLite acceptance and other combined shards ran on preceding candidates. The full all-shard combined suite was not green; stale fixtures/ongoing jobs were not counted as final acceptance. Final targeted results and final dashboard CI above are the claims for this release.

## Live acceptance versus untested journeys

Final live checks used the existing authenticated admin session. **An actual agency-scoped production session remains unverified**; synthetic assigned-creator/cross-agency/unassigned denial is covered. No production staff grant was invented to manufacture acceptance.

Known controlled Nicky fans: 318022380 and 601164546.

**New final-release manual acceptance completed**, using only account of-176774528 / controlled fan 318022380:
- Takeover revision 11, effective human ownership verified before send.
- Neutral draft survived switching to the second documented controlled fan 601164546 and back. That other conversation's control/settings were not changed. Submitted draft cleared only after accepted queue acknowledgement.
- Two neutral messages sent once through the real authenticated UI. Both later appeared as human / Sent to provider, with list/thread advancing together.
- Read-only canonical receipt checks verified account, fan, intent, attempt, provider ID, payload hash, body, one message row and sent projection. Each body has exactly one intent.
- Hand-back restored automated / takeover none / revision 12 / unresolved null. No fan input was replayed and no AI reply was fabricated.

| Reply | Intent | Attempt | Provider message | Enqueue to canonical projection |
| --- | --- | --- | --- | --- |
| One | cbd9853e-0aab-436e-b147-938b52970cf8 | d3cdabb2-b431-49d2-b733-b10bf922fbba | 11835432704265 | 13.55 seconds |
| Two | a620f467-eea5-46f9-8a98-67e0ed99890b | 5bdda6de-b30a-492c-98b0-7b659b483628 | 11835441227308 | 17.36 seconds |

Read-only receipt/hand-back SSM evidence: 17e9c5a9-82e0-4e3e-a327-dc4872be2e22. Private server evidence: manual-acceptance.json. These two controlled samples do not establish agency capacity or fresh AI behavior.

 The earlier independent release documented two accepted manual receipts (provider IDs 11829924311011 and 11830187266684), exactly one intent/message each, idempotent replay and hand-back revision 10. These older receipts are historical evidence; the distinct new final-release receipts above are this task's acceptance.

Still blocked/unverified on this final release:

1. A genuinely fresh controlled provider input beyond page one, gap/duplicate proof, incoming attachment and a fresh AI reply after hand-back. No old input replayed to fabricate success.
2. Native provider-screen confirmation and a new controlled fan input followed by an AI reply after the now-verified final-release hand-back. Historical or manual messages do not prove fresh AI behavior.
3. Controlled upload/preview/playback plus approved paid-send/purchase/unlock, ownership, ledger linkage and reporting. No approved funded purchase fixture is available; no arbitrary real fan/purchase used.
4. Fresh creator connection/import/reconnect, including actual agency-scoped session.
5. Nicky recent recovery/ledger freshness and approved current configuration; worker observation intermittently stale. Alva also lacks model/credential/delivery/catalogue prerequisites. UI now exposes these actual blockers.
6. A chosen launch agency/staff/creator cohort and its measured sender queue latency/rate envelope.

## Staffing and capacity boundary

Read-only final binding/roster check (SSM 04148f2a-7b02-4549-bd31-d5ba053972eb): all five backend bindings use tenant twin-internal. The actual agency_creators links used by agency scope are:

| Account | Backend creator | Agency link |
| --- | --- | --- |
| Nicky of-176774528 | f80d1add-d10c-4039-90f3-99e6cfc04b91 | none |
| Alva of-87030857 | 69861bbc-88b6-5985-a66f-c55fdffde4db | none |
| Naomi of-99421145 | ccf9ec63-9e46-43c8-a942-d6dd22df29f6 | none |
| Katie of-210806616 | 093386c2-53d0-5444-a8b4-d566eeb8851f | none |
| Maile of-323358988 | 5a7642bf-ca3e-5515-a159-ca6cc58a2134 | Moxy; account remains paused |

Nicky's dashboard clone is named Jesse Creator Test while its authoritative provider binding/profile says Nicky Baby. No identity/ownership was guessed from the name and no reassignment was made. Actual owner-authorized creator assignment and staff scope are therefore required before promising agency access to Nicky or Alva. The new chatter capability does not bypass absent assignment.

Read-only production roster: Moxy has 5 active memberships, one existing manager and zero chatter grants. It has eight legacy creator links and additional creator-account assignments, including test/onboarding entries. Other active agencies also have staff/creators. The earlier response "Multiple" does not identify a single approved launch cohort. Roster presence or a test-looking name does not authorize sending or grant a staff role.

The additive production migration left 14 active memberships, 2 manager flags and **zero** chatter/invite grants. Existing permission fingerprint remained 4913b725370ae12ffc1c7ce7267dc15d. can_chat is NOT NULL/default false. Both owner RPCs remain security invoker, executable only by service_role, not anon/authenticated. Transactional stage tests exercised role/CAS, invite acceptance and cross-agency/owner denial before production migration. No staffing access was broadened by deployment.

Observed last-hour incoming provider rates (all fans, not an approved capacity fixture): Maile 3/minute peak and 26/hour, Katie 1/minute and 4/hour, Alva 1/minute and 5/hour, Naomi 1/minute and 1/hour; Nicky none in the window. Most traffic was on paused creators. SQL ingestion work had Nicky 2 blocked items (oldest due ~406,105s), Maile 19 (~252,684s), Naomi 1 (~28,884s). These ages are **SQL work state, not native sender queue latency**. The two final controlled manual enqueue-to-projection observations were 13.55 and 17.36 seconds. Intended cohort peak, sender queue age distribution and fresh AI end-to-end reply latency were not proven. Per-creator 20-second serialization and 64 slots remain unchanged; no process-global concurrency was added.

**Cohort proven safe for new commercial launch: none.** Existing restricted Nicky/Naomi activation was preserved, not certified for expansion. Paid sales remain held. Broader Infloww feature parity is outside this repair.

## Deployment performed and compatible rollback

1. Captured actual running source/image/configuration and guarded exact container identity. Preserved newer production source; dirty checkouts untouched.
2. Applied/verified additive default-false staff migration and unchanged grants.
3. Deployed initial dashboard/API repair, keeping worker/sender images. Kept stopped rollback containers and private configuration.
4. Reproduced live cold-count and Launch-selector edge cases; verified final PostgreSQL cancellation/snapshot tests and final dashboard CI.
5. Built immutable final dashboard artifact; compared archive/config/RootFS/source. Built API over the verified compatible dependency image. Rehearsed real Docker replacement/rollback.
6. Final patch deployed **dashboard first**, then API, because the dashboard first accepts optional counts and the API subsequently may emit null. API still accepts v1/v2 scopes. Exact worker/sender IDs were checked unchanged. SSM cutover 3a8f83be-a994-4943-87d5-058cb191cb5e succeeded. Post-deployment API/container and admin browser checks passed.

Private deployment evidence and exact captured config are under /var/lib/twin-release/agency-crm-20261029. Production compose /etc/twin-onlyfans/media-agency-release-compose.json persists only the changed API image; worker/sender left intact. FLIRT/broker/sign-in environment unchanged.

Immediate rollback targets:

| Component | Retained stopped container | Prior source | Prior image |
| --- | --- | --- | --- |
| API | twin-onlyfans-api-1.rollback-agency-20261029-bounded | 7a4ce8fe5e418fe6d1221b545785d1ad86bcdaa9 | sha256:4cbbe8297353ee362da2aa085121ce3b38b943983e3e8a3ec233a6a3aece4d98 |
| Dashboard | next-app.rollback-agency-20261029-bounded | 324d227a1d2fc756780e35eec23994c6ecded1fe | sha256:a7a381caab390499a1998945979aca138a03c8dbd2695615e7dff9386a52ab46 |

Before rollback, check live IDs/source against deployed.json and stop on unreviewed newer work. **Restore API first, then dashboard** so the older parser never receives nullable-count responses. Use captured configuration/network aliases/ports/env/mounts/resources and restore only the API image entry in compose; retain failed/new containers with unique names. Keep schema 0098 and additive false-default staff columns. Do not restore a database dump, delete receipts/history, reset allowances, clear unknown attempts/holds, revert approvals or replace worker PR #38. Older first-repair rollback containers also retain pre-task dashboard92cf/API1dcb, but require their own compatibility review.

## Files changed

Dashboard list is the review diff against preserved release 92cf2479. Backend list is the actual incremental diff against preserved live worker/API 1dcb9846, not attribution of independent prior repairs. Full review branch includes the preserved upstream changes.

### Dashboard

- `app/agency/team/page.tsx`
- `app/api/onlyfans-controls/accounts/[accountId]/conversations/[conversationId]/route.ts`
- `app/api/onlyfans/accounts/[accountId]/conversations/[conversationId]/handover/route.ts`
- `app/api/onlyfans/accounts/[accountId]/conversations/route.ts`
- `app/onlyfans/inbox/page.tsx`
- `app/onlyfans/launch/automation/page.tsx`
- `app/onlyfans/launch/layout.tsx`
- `app/onlyfans/launch/memory/page.tsx`
- `components/AgencyTeamForm.tsx`
- `components/Sidebar.tsx`
- `components/onlyfans-launch/AbcWorkspace.tsx`
- `components/onlyfans-launch/a-shared.tsx`
- `components/onlyfans-launch/automation/BAutomationPanel.tsx`
- `components/onlyfans-launch/memory/BMemoryPanel.tsx`
- `components/onlyfans-launch/personality/PersonalityScreen.tsx`
- `components/onlyfans-launch/readiness/ReadinessScreen.tsx`
- `components/onlyfans-preview/AccountIdentity.tsx`
- `components/onlyfans-preview/AccountsView.tsx`
- `components/onlyfans-preview/AllFansControl.tsx`
- `components/onlyfans-preview/CampaignManager.tsx`
- `components/onlyfans-preview/InboxView.tsx`
- `components/onlyfans-preview/NativeWorkspaceView.tsx`
- `components/onlyfans-preview/OnlyFansWorkspace.tsx`
- `components/onlyfans-preview/OverviewView.tsx`
- `docs/onlyfans/AGENCY-CRM-LAUNCH-20261028.md`
- `lib/agency-identity.ts`
- `lib/agency-team-command.ts`
- `lib/onlyfans-preview/actions.ts`
- `lib/onlyfans-preview/data-source.ts`
- `lib/onlyfans-preview/fixture-data-source.ts`
- `lib/onlyfans-preview/server-data-source.ts`
- `lib/onlyfans-preview/types.ts`
- `lib/onlyfans/agency-scope.ts`
- `lib/onlyfans/backend.ts`
- `lib/onlyfans/fan-financials.ts`
- `lib/onlyfans/inbox-drafts.ts`
- `lib/onlyfans/inbox-search.ts`
- `lib/onlyfans/launch/automation/b-automation-client.ts`
- `lib/onlyfans/launch/b-common.ts`
- `lib/onlyfans/launch/b-native-routes.ts`
- `lib/onlyfans/launch/b-route.ts`
- `lib/onlyfans/launch/ledger-model.ts`
- `lib/onlyfans/launch/native-backend.ts`
- `lib/onlyfans/launch/native.ts`
- `lib/onlyfans/read-recovery.ts`
- `lib/onlyfans/reads.ts`
- `lib/onlyfans/route.ts`
- `middleware.ts`
- `scripts/check-agency-launch.tsx`
- `scripts/check-inbox-drafts-dom.tsx`
- `scripts/check-jess-kate-access.ts`
- `scripts/check-login-pages.mjs`
- `scripts/check-moxy-owner-access.ts`
- `scripts/check-onlyfans-agency.ts`
- `scripts/check-onlyfans-chatter.sql`
- `scripts/check-onlyfans-crm-video.ts`
- `scripts/check-onlyfans-http-browser.mjs`
- `scripts/check-onlyfans-overview-browser.mjs`
- `scripts/check-onlyfans-ui.tsx`
- `scripts/check-sidebar-layout.tsx`
- `scripts/check-tether-access.ts`
- `supabase/migrations/20261028125157_onlyfans_scoped_chatter.sql`

### Backend incremental files

- `.github/workflows/onlyfans-launch-integration.yml`
- `docs/onlyfans/ACCEPTANCE-DASHBOARD-SHA`
- `docs/onlyfans/AGENCY-CRM-LAUNCH-20261028.md`
- `scripts/check_recent_inbox_pagination.py`
- `service/native_pilot/tools/load_test.py`
- `service/onlyfans_commands/api.py`
- `service/onlyfans_commands/launch_api.py`
- `service/onlyfans_commands/launch_control_api.py`
- `service/onlyfans_read/agency_scope.py`
- `service/onlyfans_read/api.py`
- `service/onlyfans_read/contract.py`
- `service/onlyfans_read/repo.py`
- `service/onlyfans_read/service.py`
- `service/onlyfans_runtime/connections.py`
- `service/onlyfans_runtime/launch_history_memory/memory.py`
- `service/onlyfans_runtime/launch_operations.py`
- `service/onlyfans_runtime/launch_personality/workflow.py`
- `service/onlyfans_runtime/launch_personality_credentials.py`
- `service/onlyfans_runtime/media_delivery.py`
- `service/onlyfans_runtime/recent_inbox.py`
- `service/onlyfans_runtime/store.py`
- `service/onlyfans_runtime/text_delivery.py`
- `service/tests/onlyfans_upgrade_fixture.py`
- `service/tests/test_agency_launch_contracts.py`
- `service/tests/test_fastt_scope_agreement.py`
- `service/tests/test_fastt_scope_manual_pg.py`
- `service/tests/test_native_pilot_load_pg.py`
- `service/tests/test_onlyfans_launch_controls_pg.py`
- `service/tests/test_onlyfans_native_memory_pg.py`
- `service/tests/test_onlyfans_recent_inbox_pg.py`
