# Contextual offers: reconciled release status
30 October 2026. This report supersedes the earlier COMPLETION.md deployment, enabled-state, sync-blocker and whole-compose rollback instructions.

## Live enablement update, 08:24 UTC

After Preview and the absence of a verified contextual send were explained, the user explicitly requested On. Maile alone was switched from shadow to live through the authenticated audited configuration API at 2026-10-30 08:24:50.405284 UTC, revision 10 to 11. SSM: 52e3751f-2c9a-4457-bd36-ad60fbf768f0. Command key: 02542b3d7ca953dee8f01d823e3cfec835d668e61a80c2c67c31c86c44ce90f8.

The audit confirms fasttContextualSalesMode was the only changed field. Current purchase coverage passed the unchanged freshness check, with cutoff 2026-10-30 08:19:37.883112 UTC. Existing 608 automated conversations, six takeover conversations, fan selection, 600-cent daily model budget, prices, caps, on-request sales, pacing and formatting were preserved. Other creator modes were unchanged; Nicky remains excluded. No test message, fan activation, reconnection or purchase occurred.

The setting is live and read back successfully. There were zero contextual origin rows at that check, so no contextual provider send or purchase/unlock cycle is claimed. The earlier lack of natural Preview decisions remains a validation limitation. All deterministic eligibility, purchase, content, duplicate, takeover, revision and transport checks remain enforced. Contextual offers Off is the independent kill switch.

## Status
| Item | Evidence/status |
|---|---|
| Contextual code | Implemented in backend PR 56 and dashboard PR 335, retained by the subsequent pacing release |
| Regression testing | Passed focused contextual, delivery, takeover, pricing, manual-media and purchase-sync tests; limitations below |
| Production deployment | Feature source 0b4e7eaf0afd87846723f134313d5db2eb78727f is present in the current API/sender overlays |
| Enabled | On (live) for Maile only since 08:24:50 UTC, verified at revision 11 |
| Live contextual decision | Not observed in reviewed traffic |
| Contextual provider send | Not verified |
| Purchase/unlock cycle | Not verified; recent transaction coverage is separately verified |
| Completion | Code, deployment and requested enablement are complete; live send/purchase acceptance remains incomplete |

Contextual offers means suggesting or offering relevant, actually available creator content during an existing AI conversation, even without an explicit buying request. The initial implementation selects ordinary chat or one matched paid card; it does not emit suggestion-only messages.

## Concurrent work and source identities
The overlap review found the deployed reply-pacing implementation from backend PR 59 and dashboard PR 337, and alternative, undeployed pacing/format PRs 57/58 and dashboard PR 336. The alternate implementation was not layered onto production. The compatibility patch's source/evidence parents were 794bb51fbca9ce932b4907d4f93a4aa9c56c0277 and e38cbb16b66f9eaaadfb10a6ed0d25e3689f26cd. The later backend evidence-only head ffe96972de51e531af4e0af5b8f4f0db48380ca0 records Maile formatting enabled at 08:06:08; its deployed application source remains d745017852e33df3c7a01f305f116084df2ef7bd.

The current sender retains the contextual constructor/authority, one winning normal reply or paid card, frozen pricing/media snapshots and final takeover checks. Core contextual modules were byte-identical to the deployed feature. Running combined-image tests passed 75 guard/pacing/format cases and eight synthetic native turns, including formatting on/off, fixed/dynamic pricing and Preview behavior. No customer traffic was generated.

One real compatibility regression was reproduced: the concurrent API returned nativeReplyPacingEnabled and nativeReplyFormatEnabled to old strict dashboard parsers without negotiation. Twin's current dashboard accepted them, but the separate older FLIRT dashboard could reject the configuration response. Backend PR 61 adds independent replyPolicy=1 negotiation; dashboard PR 338 explicitly requests it alongside contextual=1. Only the API response module and dashboard transport/test change. Configuration values, save rules, activation, sender and worker are unchanged.

| Change | PR | Runtime source |
|---|---|---|
| Contextual feature | [backend 56](https://github.com/twinagency/twin-onlyfans-backend/pull/56), [dashboard 335](https://github.com/twinagency/twin-dashboard/pull/335) | Backend 0b4e7eaf0afd87846723f134313d5db2eb78727f; original frontend c3576f07f90a8197c3b5f95a9e62e093b827415e |
| Preserved pacing release | [backend 59](https://github.com/twinagency/twin-onlyfans-backend/pull/59), [dashboard 337](https://github.com/twinagency/twin-dashboard/pull/337) | d745017852e33df3c7a01f305f116084df2ef7bd / 2e4dfea7acbc5fa3fe14b64dc2d3bb981b5da7db; actual sender/API overlays labelled 208785-reconciled |
| Independent purchase-sync repair | [backend 60](https://github.com/twinagency/twin-onlyfans-backend/pull/60) | ce1ec7a052a1ae434c67d8fb15fc3124650a4af0 |
| Settings compatibility | [backend 61](https://github.com/twinagency/twin-onlyfans-backend/pull/61), [dashboard 338](https://github.com/twinagency/twin-dashboard/pull/338) | 5727cb6ca07b3811782e881285bebfd9268b8454 / 8cde4384ab5f8af5e45a838d1d8c81ff41d9a245 |

These are overlay source revisions, not a claim that a historical repository checkout equals every production file. The attribution fix 6a8c99ec2cb096422f4054205120adaa4a6f1294 and later FASTT v2 repairs were retained.

## Current running images
Host twin-dashboard-prod, instance i-09a9dd3bd749643a5, ap-southeast-2. Compatibility API promotion SSM 5aa5140c-69ed-4bd6-bfec-f4e47268ff9f succeeded. The second dashboard promotion SSM d25f330f-f396-4913-bd8a-090be143eb84 succeeded. Both dashboard consumers were updated before the API.

| Container | Immutable running image |
|---|---|
| twin-onlyfans-api-1 | sha256:17ff96b2f9e352f5daad108b66caebd932af0c1092a2f8c41eec7a52054e26ca |
| twin-onlyfans-fastt-supervisor-1 | sha256:2fa1dd0604814eb0a287e67599d4215a648169c3078c1ac0c871069656c01e8c |
| twin-onlyfans-worker-1 | sha256:ce1bcd5eeade5381890e633c3127cd0787b48553f43e330a6925e02ab84545e5 |
| next-app and next-app-of | sha256:724e2615043dd4b1f6a9ad2a20c6f44d5bad9887277d4d24c395aaf3bbce93f2 |

Health checks passed. Live API reads verified legacy responses omit both new control groups; contextual-only, reply-policy-only and combined queries each expose exactly their negotiated fields. On-request sales remains true. The compatibility deployment changed no setting. A concurrent operator write advanced Maile to revision 10 and enabled reply formatting; the final read preserved pacing=true, formatting=true and contextual=shadow. Combined tests included formatting on.

## Files and schema
The feature adds the per-creator mode and auditing in onlyfans_runtime/configuration.py and contextual_mode.py; explicit trigger/grounding in automations/_sell_signal.py and contextual_offers.py; routing in ai_chatter.py and native_pilot/engine.py; shared sell_lane.py and pack_sender.py guards; durable origin and final authority in native_pilot/contextual.py with runner/service/recovery/postsale integration; configuration API negotiation; and the existing dashboard creator settings UI/client/schema. Creator-approved persona and separate fan memory are reused.

Migration 0101_contextual_offers adds durable contextual origin state. No fan activation, price, allowance or account configuration is migrated. Turning Off does not delete evidence. Purchase-sync and compatibility follow-ups have no schema migration.

The separate sync patch changes provider_read_budget.py, provider_connector.py and ledger.py, with deterministic fairness/date-filter/migration tests. The current production connector contained newer repairs than its repository counterpart; its original blob 5acb65e4630879c067356a409959a6ab8d22b3b9 was preserved and only the date-parameter block patched. Historical scans restart old incompatible cursor scopes without deleting financial observations.

The compatibility patch changes only service/onlyfans_commands/configuration_api.py and adds service/tests/test_configuration_negotiation.py. Its frontend companion changes lib/onlyfans/configuration-backend.ts and scripts/check-onlyfans-configuration.ts.

## Validation
- Feature release run [41011316445](https://github.com/twinagency/twin-onlyfans-backend/actions/runs/41011316445): all five release groups passed; 47 contextual cases, four native turns, 48 PostgreSQL configuration/migration cases, 35 manual cases, 92 ledger cases and three historical upgrades.
- Combined actual sender image: 75 guard/pacing/format checks and eight native-turn cases passed. Evidence at /var/lib/twin-release/contextual-overlap-review-20261030.
- Compatibility API: six HTTP cases. The untouched current API reproduced four failures; patched API passed all six. Existing contract, auth and save mechanisms are retained. Local frontend transport/parser test passed.
- Compatibility release run [41022225806](https://github.com/twinagency/twin-onlyfans-backend/actions/runs/41022225806): all five PostgreSQL release groups passed.
- Date-filter release run [41017077835](https://github.com/twinagency/twin-onlyfans-backend/actions/runs/41017077835): all five groups passed, including 160 ledger/provider checks. Local adapter/contextual: 111 passed. Fairness budget cases: 22 passed.
- Actual production-connector harness initially had 149 passes and 11 fixture failures. Eight reproduced without the patch because the disposable image lacked its existing read_failures module; three used old migration fixtures. Correcting the harness yielded 69 passing affected adapter/date/migration cases. Runtime behavior was not weakened.
- Dashboard compatibility run [41022241688](https://github.com/twinagency/twin-dashboard/actions/runs/41022241688) passed type/build/checks and creator/upload/access tests, but production rejected its feature-branch workflow. The identical established main workflow with the explicit tested ref is [41023363967](https://github.com/twinagency/twin-dashboard/actions/runs/41023363967). Protection rules were not changed.
- The approved dashboard release passed type/build/checks, upload/access, serving-revision verification, live earnings/private-media recovery and signup/upload-boundary checks. Its final catalogue audit completed at 08:16 UTC: 1,309/1,313 passed; four invalid-video probes failed, with zero unchecked items. Comparing creator/video IDs, stage and reason against release 41011459654 confirmed exactly the same four failures. They remain unresolved; the overall workflow is failed and its subsequent public-homepage check was skipped. The deployment and live compatibility checks completed successfully before that audit.
- General backend CI is not completely green: three bridge checks lack their cross-repository helper/tsx/Windows runner. Combined/offer acceptance lacks the explicitly provisioned paired dashboard source. These did not execute successfully and are not reported as passing. Two historical sender allowance/pace-stop fixtures previously failed identically on the unchanged pre-release sender.
- No new real fan test messages, purchases, physical-device settings walkthrough or current end-to-end purchase/unlock test was performed.

## Freshness, traffic and rollout scope
At 08:11:24 UTC, all three connected recent scans passed the unchanged 345-second freshness guard and were idle without errors:

| Creator | Last complete/window cutoff UTC | Recent rows/pages offset |
|---|---|---:|
| Maile | 2026-10-30 08:09:04.768521 | 39 |
| Naomi | 2026-10-30 08:07:58.835831 | 0 |
| Alva | 2026-10-30 08:10:56.412363 | 6 |

Successful cutoffs advanced repeatedly from the earlier 07:31 and 07:48 observations. The frozen October 29 recent windows are caught up. Historical backfill remains separate: Maile advanced from offset 1220 to 1300 and Alva from 760 to 900 between 08:02 and 08:11, both still running/deferred; Naomi was idle at 1963 with historical completion through October 29 21:25:56. The total remaining historical count is unknown. Nicky's retained history job is unchanged since its disconnection; it was not resumed.

This establishes recent transaction-sync progress under ongoing message reads. It does not prove a new purchase/unlock cycle.

Maile (of-323358988) was the sole Preview creator and is now the sole live contextual creator following the explicit 08:24 enablement request. The audited contextual change was revision 7 to 8; the independent pacing change later advanced it to 9 while retaining Preview. Existing 608 automated conversations, five takeover conversations, all-fans selection, 20-second throttle and 600-cent daily model budget were retained at activation. New conversations remain outside any new activation by this feature. Pacing is on for Maile. Formatting was off at the 08:02 read, then a concurrent writer enabled it at revision 10 before the compatibility API verification; both are retained. The 08:02:14 live check still showed revision 9, 608 automated conversations and now six takeover conversations; no takeover was cleared. Values must be refreshed before a future write.

Naomi (of-99421145) remains contextual Off. Nicky (of-176774528) remains paused/disconnected_by_owner and is excluded. Katie (of-210806616) remains paused. Alva (of-87030857) remains held for no approved manifest items; no incidental sales/model repair was made.

At the reviewed 07:00 UTC onward traffic sample, Maile had one incoming message at 07:39:36 and Naomi one at 07:02:54. Both belonged to human-controlled conversations. No contextual decisions or origin rows were observed. This is insufficient live Preview evidence, not a reason to override takeover. Older delivery-unknown attempt 543 from October 26 remains held without a provider message ID; uncertainty was not cleared or resent.

The user explicitly requested the live switch after the Preview limitation was explained; it is now enabled for Maile alone. Continue observing naturally occurring decisions and guards with fresh purchase coverage. Keep Maile as the sole creator and preserve existing eligibility/caps; do not activate fans or reconnect anyone. Verify matching content/price and an actual provider receipt separately from purchase/unlock evidence. Do not claim conversion uplift. Stop via Off immediately for any duplicate, wrong creator/content/price, takeover violation, ignored-offer chase or unintended activation.

## Kill switch and rollback after concurrent changes
Set Contextual offers to Off in the existing creator editor, or POST only fasttContextualSalesMode: off through the existing audited configuration API using a freshly read revision/fingerprint and stable command key. Final transport rechecks invalidate queued contextual actions. On-request sales remain independent. Do not toggle qualification_gate_enabled, prompt_sell_catalog or hook_upsell_enabled to accomplish this.

Final read-only verification SSM 3c03ecf2-5832-410a-a579-c1b7c6cf6661 succeeded at 08:11 UTC, confirming all images, settings, recent coverage and no contextual origins.

Fresh compatibility baseline: /var/lib/twin-release/settings-compat-20261030. It retains per-container before.json, API source, tests, compose snapshots and result records. API parent is sha256:ca09c9079b72e045795ee35ce4ab921ac4b7398b95dd885488179de66f6c687b. Frontend parent is sha256:60c4edf7947e3af3cbe33454b2df90db508d58572a7a40bf3d5698ec2a33d1fb. Restore the API first if reverting this pair, then the dashboards. The older API ignores replyPolicy=1, so newer dashboard requests remain compatible during rollback.

For API rollback, inspect current identities first. Replace only services.api.image in the CURRENT /etc/twin-onlyfans/media-agency-release-compose.json with the captured API parent, then run:
    docker compose -p twin-onlyfans -f /etc/twin-onlyfans/media-agency-release-compose.json up -d --no-deps api
Preserve sender/worker and all other current manifest fields. The exercised deploy script has automatic health-failure rollback limited to that service. Restore frontends from their exact captured environment/ports/network using the retained predecessor image/container; do not copy old unrelated environment values into a later deployment.

Date-filter rollback baseline: /var/lib/twin-release/ledger-date-filters-20261030, prior worker sha256:9adf223bae627176b621399a56ca239716125f4df5c5e41263502a8621a769d2. Fairness baseline: /var/lib/twin-release/ledger-fairness-20261030, prior worker sha256:3257dba4b75a0a0bbf43a9a0b99a17e04a0af11f76b4105a770b9667fd17c0df. Restore only services.worker.image, then the same compose command with worker. No schema downgrade. A rollback may make purchase state stale again; contextual guards continue to refuse dependent actions.

The original feature baseline remains under /var/lib/twin-release/contextual-offers-20261030-astra-6172d27. **Do not restore its whole compose or old sender/API over the pacing release.** Use the mode kill switch for contextual rollback and retain 0101 evidence. Any future code removal must be reconciled with the newer sender source first. Never start the disabled legacy fastt-runner and never clear a delivery-unknown hold to force a retry.
