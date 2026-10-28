# Agency CRM launch repair, 28 October 2026

## Release status

Implementation candidate only. No production deployment, schema change, quarantine release, purchase relinking, approval change, or new staff grant was performed. Commercial readiness and Infloww parity are not established. No creator/staff cohort has completed live acceptance.

The dashboard candidate preserves release/twin-20261028 through 52b2663cacbe507dc609c39d9042a18f7930cdce, including the newer inbox sync warning and connection-status focus fixes. Its preceding audit snapshot was 6765b61f607280125f600271a2433d408ce3e39b. The backend candidate is based on PR #38 head 3f7fbd239184aa4b67f610271dc02df872cdfe7f, preserving the recent-inbox continuation implementation and regression script; codex/onlyfans-of-b1 was still 0af4132ee90bb1886ec64367f1445e337ba5d14d. PR #38 remained unmerged when inspected.

Activation and handover records in backend docs/onlyfans were read before runtime edits. The documented independently deployed worker overlay means the application branch alone cannot identify the running release. Remote command attempts to the connected laptop timed out. Actual dashboard/API/worker/sender image digests, server local changes, runtime schema revision and sender effective configuration remain unverified. Do not replace any running image using these branch names alone.

## Implemented changes and causes

| Area | Cause or demonstrated defect | Candidate repair |
| --- | --- | --- |
| Draft loss | Composer-local state and broad result resets lost conversation drafts and could clear a different draft on a late acknowledgement. | Document-memory drafts keyed by server-derived principal/agency, account and conversation. Retain text and catalog selection through chat/remount/SPA navigation. Clear only the successfully acknowledged, non-simulated captured revision. Newer edits and other conversations survive. Eight-hour expiry; no disk persistence; a full logout/reload drops document state. |
| Creator context | Section navigation omitted account/creator context. | Preserve authorized account in sidebar and launch section navigation, validate selectors against authorized accounts, label acting creator in native headers/composer and notes. |
| Search | Browser filtering only searched already loaded rows. BFF cursor validation was limited to 64-character request IDs. | Account-scoped server search over fan name, username and fan ID, with server unread/attention/AI filters before pagination. Message content is not searched. Cursor accepts bounded base64url and binds search/filter/binding. |
| Fan spend | UI had no canonical financial projection. Asset/purchase rows would double-count receipts. | Project canonical transaction aggregates once per transaction, separated by currency/payment status with integer gross/net, timestamps, partial coverage and fan commerce hold. Financial failure displays unavailable and preserves transcript availability. |
| Notes | Audited memory correction existed outside the inbox. | Embed the existing BMemoryPanel, fix it to the selected authorized account/fan, preserve correction reasons/version/idempotency and backend permissions. No parallel notes store. |
| Staffing | Manager/read-only roles could not express messaging work. Manager policy also ignored explicit legitimate membership grants for some agencies. | Add default-false can_chat to memberships/invites and explicit chatter role. Versioned agency scope supports v1 and v2. Enforce assigned-creator scope and action allowlist at dashboard routes, transport, backend middleware, commands and manual-send authority. Chatter may request takeover, hand back, submit manual messages/permitted media and audited notes; broader configuration, credentials, bulk AI, campaign, backfill and team writes remain manager-only. Existing explicit manager grants and legacy named agency policies remain. No membership was elevated. |
| Unlinked purchase counter | Operations counted ingestion time, while the actual commerce guard uses purchase occurrence time relative to the current binding. Old purchases imported today inflated current-binding blocker counts. | Count using occurred_at to match the guard. Historical rows remain evidence, unlinked rows stay unlinked, and commerce ownership/unknown-send/duplicate guards remain active. |
| Scan wording | Ledger intentionally has running rows without a lease between pages and during backoff. | Explain in-progress/waiting/backoff and the retry reason. Preserve lease/CAS/recovery gates. This does not establish whether a particular live scan is orphaned. |
| Configuration approval | A fingerprint mismatch gave no field-level explanation. | Derive exact before/after differences from the immutable configuration-change chain and display actor/time/chain completeness. Missing chain stays unknown. Never reapprove/reapply automatically. |
| Readiness | BFF flattened transport/status/schema/account errors into one generic message; UI lacked bounded timeout and last success/retry state. | Distinguish connection/timeout/refusal/version/envelope/account errors, retain honest last-success and attempt state, and correlate safe backend exception evidence references without exposing SQL parameters. The underlying production outage is still unresolved. |
| Health | Connection issues were presented as all operational issues. | Separate connection, history freshness and payment import indicators; state sending readiness is not verified on the overview. Preserve newer sync-warning/connection navigation fixes. |
| Bulk AI | Label implied all/new fans would be enabled. | Say eligible existing conversations; explicitly retain new-fans-off policy. |
| Intermittent audience/actions reads | A transient first GET failure required manual recovery. Exact live cause is unproven. | One bounded retry for network/502/503/504 reads, honoring abort and Retry-After; no retry for authorization, 429 or writes. Persistent failures remain visible. |
| Login CI | Isolated production smoke relied on an unavailable external session service. | Provide a local revocable-session HTTP fixture while exercising actual production authentication RPC behavior. Assert valid redirect, wrong password, copied revoked token denial and fail-closed session-store outage. No production auth bypass. |

## Live evidence and unresolved blockers

An authenticated admin browser reproduced Nicky readiness failure at /api/onlyfans-launch/abc/accounts/of-176774528/readiness: schema of-launch.v1, outcome unavailable, error "Current account evidence could not be verified." The page also displayed the failure and Retry. Approximately ten seconds elapsed; this alone does not prove a timeout root cause. The new diagnostics have not been deployed or validated against this failing runtime. Alva post-fix readiness and an actual agency-scoped session remain unverified.

At 2026-10-28T13:32:31Z, Nicky's settings response showed verified provider connection, historyRecoveryRequired true, lastCompleteSyncAt null, runtimeEnabled true, delivery policy revision 5, enabled true, media enabled true and 200/500 units. Personality revision 3 was approved against configuration revision 2/fingerprint d3a6bdc11e30c9d40092386e775cfd2c4fcf99774804b321fa6935a400d8297d, while configurationCurrent was false and blocker approved_configuration_changed persisted. The full saved persona was not truncated and exactly matched the approved persona; saved creator facts and reviewed facts were empty. Saved model was openrouter/deepseek-v4.1-flash, override null, cap 1000 cents/day, language null and three sales permission settings null. This proves no persona-text discrepancy on those surfaces; it does not establish the exact remaining fingerprint difference or effective sender configuration. The candidate exposes audit-chain differences when available. No approval was changed.

The audit's 141 history conflicts/two blocked items and 1,732 unlinked purchases were not reconciled against authoritative provider evidence. No conflicting evidence was deleted or quarantine narrowed. The counter correction is not evidence that purchases are owned or holds may be cleared. Exact held fan/media scope and any orphaned scan require runtime/provider evidence and an audited repeatable reconciliation. Keep affected sending/sales blocked.

Read-only production membership inspection found Moxy with eight assigned creators and five active staff; Multiple was the only launch-agency clarification supplied. Other agencies also have creators/staff. The production membership schema lacked can_chat. The candidate therefore supplies a migration and requires explicit scoped role assignment; it does not claim Moxy or all agencies are live-tested.

## Validation actually completed

Against the final merged dashboard source: TypeScript checking, production Next build and isolated production login smoke passed. The newer upstream connection/sync-warning regression was retained and passed. After the final upstream focus-only change, TypeScript/sync-warning/build were repeated.

Targeted dashboard checks passed:
- agency launch contracts: 6 tests, covering scoped draft revisions, canonical money, server-search transport/long cursor, narrow chatter writes, bounded read recovery and readiness errors;
- React DOM draft test: text and selected content survive chat remounts; matching-revision clearing;
- launch controls: 17 tests; launch B/native memory: 24 tests;
- launch access: 155 assertions; OnlyFans read contracts: 116 checks;
- agency, UI, controls, bulk AI, inbox refresh, thread pagination and session revocation scripts;
- AI live script through equivalent node --import tsx invocation. The npm/tsx wrapper hit a sandbox IPC EPERM and is not counted as a pass itself.

Backend: six new agency-launch contract tests passed, including actual SQLite tenant/creator denial, search beyond the first 150 fans, escaped wildcard/search cursor binding and historical purchase counter/guard consistency without relinking. One existing scope parser test passed. Exact PR #38 recent-inbox script passed short-page continuation, deferred retry and connection replacement cases. Python compile checks passed.

The additive migration and copied private invoker RPCs were exercised transactionally on the designated stage Supabase project: defaults, chatter role/CAS, owner/cross-agency denial, invite acceptance and private RPC permissions passed. The test schema was rolled back and its absence verified. Production was read-only.

Limits: the full backend suite was not completed because a legacy lane_config dependency was absent; full PostgreSQL runtime/concurrency suites were not run. Browser Chromium installation failed, so DOM checks used React/jsdom, not a full browser acceptance run. Historical test results do not substitute for this candidate's validation.

## Acceptance still required

No designated controlled creator/fan IDs or approved purchase fixture were supplied. No arbitrary fan was contacted, no purchase was made and no real sending was enabled. All of the following remain untested on this candidate:
- both Alva and Nicky readiness success plus actual agency-scoped session;
- genuinely new provider messages beyond page one with gap/duplicate proof;
- incoming attachment, takeover, two manual replies/provider receipts, hand-back and fresh AI reply;
- media upload/preview/playback and paid send/purchase/unlock/ownership/ledger/reporting;
- fresh creator connect/import/reconnect;
- provider-backed history reconciliation and held fan/media inventory;
- intended cohort peak arrival rate, queue age and latency.

The per-creator 20-second serialization and supervisor's 64 slots are architecture limits, not capacity measurements. Neither was raised; no concurrency was added around process-global state.

## Deployment sequence and compatible rollback

1. Obtain working server command access. Capture current branch/local changes, image digests and BUILD_SHA for dashboard/API/worker/sender, migrations, environment compatibility and PR #38 overlay. Compare every running component with the candidate; preserve any later production changes. Stop if the source pair is incomplete.
2. Back up the database and record memberships/invites. Apply the additive dashboard migration, leaving all can_chat values false. Retain existing grants. No data reconciliation or approval write is part of this migration.
3. Deploy the backend candidate first, including API/worker/sender from the same verified source and PR #38 ancestry. It accepts legacy v1 and new v2 scopes. Verify endpoint version/identity behavior and unchanged quarantine/unknown-send/duplicate protections.
4. Deploy the paired dashboard candidate. Verify the new v2 handshake, denied cross-agency/unassigned-creator access, useful readiness evidence and all intended configuration/schema versions. Grant chatter only to explicitly reviewed memberships/creator assignments.
5. Complete the controlled acceptance list, including actual agency session and rate/queue observations. Enable only the proven cohort. Broader feature parity remains outside tonight's release.

Rollback: restore the compatible preceding dashboard first if reverting the backend to v1-only, then restore captured API/sender digests. Keep the additive false-default schema. Restore/revoke explicit chatter grants through audited membership operations if needed. Preserve PR #38 on the worker; do not replace its independently deployed fix with the old application branch. Never undo transaction/history evidence or clear quarantine to roll back. If a previous backend cannot read the additive schema/other migrations, use the captured compatible pair rather than assuming compatibility.

Working runtime command access and designated controlled creator/fan fixtures are the next concrete dependencies. Source changes alone do not clear the launch blockers.

