# Transaction-sync repair and Preview rollout

30 October 2026. This updates the sync-blocker and enabled-state sections of COMPLETION.md.

## Implemented and deployed

Separate review: https://github.com/twinagency/twin-onlyfans-backend/pull/60

- Fair read scheduling: a9dcf7753d8880f71388332c230e4073915409ab. A waiting ledger scan gets the next available read turn; the 45-second lease, shared budgets, provider backoff and reply reserve remain enforced.
- Correct provider query: ce1ec7a052a1ae434c67d8fb15fc3124650a4af0. `startDate`/`endDate` replace ignored `start`/`end`; covering second-precision bounds are filtered back to the exact frozen interval. Old pagination scopes restart safely, retaining immutable financial observations and preventing double counting.
- Worker image: `sha256:ce1bcd5eeade5381890e633c3127cd0787b48553f43e330a6925e02ab84545e5`.
- Promotion SSM: `2dcab778-f6d4-4d8b-81f0-71e06740b9aa`, successful.
- This repair changed only the worker. The independently deployed reply-pacing release changed API and sender images; the overlap review preserves those images. Dashboard is unchanged by this repair. No schema change.

The running connector contained newer repairs than the repository version. Its original blob `5acb65e4630879c067356a409959a6ab8d22b3b9` was retained on-host and only the exact date-parameter block changed. The complete historical repository connector was not deployed over it. The other runtime edit is the ledger cursor-scope version; the existing fairness overlay is retained in the parent image.

## Tests and provider evidence

Release CI https://github.com/twinagency/twin-onlyfans-backend/actions/runs/41017077835 passes all five groups, including 160 ledger/provider checks. Local adapter/contextual run: 111 passed. Initial production-connector harness: 149 passed, 11 failed. Eight failures reproduced on the untouched connector because the disposable image lacked its existing `read_failures` module; three used outdated historical migration fixtures. Adding that production module and the exact committed fixtures resolved every failing case: 69 affected adapter/date/migration checks passed. Production code was not weakened to satisfy the harness.

Read-only, account-bound provider probes used the existing shared ledger lease and honored backoff. Old query parameters returned 20 transactions from November 2025 to February 2026 for an October 2026 window. Correct parameters returned zero for October 27–28 and one known transaction for February 10–12, dated February 11. These prove query filtering, not a new sale or purchase/unlock cycle. No real fan test messages or purchases were made.

## Successful recent coverage

Read-only check at 07:48:47 UTC on 30 October 2026 found all three connected scans fresh under the unchanged 345-second guard. Maile completed through 07:47:57.974181, Naomi through 07:47:20.950230, and Alva through 07:45:12.257317. All were idle with no scan error. Their earlier frozen October 29 windows have caught up to current time and repeated recent scans now complete. This proves recent transaction coverage, not that all historical backfill is finished or that any new purchase unlocked.

## Preview scope

Maile, `of-323358988`, was changed from Off to Preview (`shadow`) through the audited configuration API, revision 7 to 8. Command key: `2a3f422f5d69e016e996c83341477c7287ab679d25f4f64563305cab64e92e65`. Existing 608 automated conversations, five takeover conversations, all-fans selection, 20-second throttle and 600-cent daily model budget were retained. Only `fasttContextualSalesMode` changed. No fan activation or account connection changed.

Naomi remains Off; Nicky remains disconnected and excluded; Katie remains paused; Alva remains held for no approved manifest items. No incidental account setup repair was performed. Live contextual sends are not enabled. No provider contextual-send or purchase/unlock claim is made.

## Kill switch and rollback

Use the existing Contextual offers selector or audited revision/fingerprint configuration API to set `fasttContextualSalesMode` to `off`. Queued contextual actions recheck this before transport. Established on-request sales remain independently configured.

Date-filter rollback baseline: `/var/lib/twin-release/ledger-date-filters-20261030/worker.before.json` and `compose.before.json`. Previous worker image: `sha256:9adf223bae627176b621399a56ca239716125f4df5c5e41263502a8621a769d2`. Restore only the worker image in the current compose, then `docker compose -p twin-onlyfans -f /etc/twin-onlyfans/media-agency-release-compose.json up -d --no-deps worker`. Preserve any later unrelated compose changes. Old cursor scopes are rejected in either direction, so rollback restarts scans without deleting transactions.

Fairness rollback baseline: `/var/lib/twin-release/ledger-fairness-20261030`, previous worker image `sha256:3257dba4b75a0a0bbf43a9a0b99a17e04a0af11f76b4105a770b9667fd17c0df`. Neither repair needs a schema downgrade. Never start the disabled legacy fastt-runner. Do not restore the old whole-compose snapshot in COMPLETION.md: that would erase the subsequent reply-pacing release. Contextual behavior rollback is its independent Off switch, with schema and evidence retained. `repair-ledger-dates.sh` records the exercised promotion preflight and automatic failure rollback.
