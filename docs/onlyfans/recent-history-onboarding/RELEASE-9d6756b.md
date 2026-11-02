# Backend release verified, 2 November 2026, 23:24 Brisbane

Application release: 9d6756b20063dc7d481f59acfb1f26c65336c357. This release extends the existing onboarding/importer; the separate conversation store, Moxy, concurrent-reply work, dashboards and controls are preserved.

| Component | Deployed image |
| --- | --- |
| Worker, importer, learner | sha256:6afd1ad6bfc1030cbb0c7cd72d675753cde487b9de64bf0e333b02d89b4831f2 |
| API | sha256:87bc296eb275e2c220c3daa2726d34c5e9d8c4faa77455b5bf7fd77e8c00f439 |

All four services independently read back running with zero restarts and OCI revision 9d6756b20063dc7d481f59acfb1f26c65336c357. API health returned 200. Schema remains 0110_recent_history, fixed cutoff remains 2026-10-03T09:44:23.165525Z, and Maile replies remain disabled. Deployment interruption: 33.789 seconds. Both dashboard images and the stopped supervisor were preserved.

## Scope and qualification

- Correct recent-memory errors to count only failures with pending recent sources. Historical flags remain stored and separately reported.
- Record message-thread HTTP 404/410 as unavailable coverage, preserve cursor and retry hourly. Do not infer deletion or count failure as completion.
- Add automatic enrollment of newly provisioned creators behind ONLYFANS_RECENT_HISTORY_NEW_ACCOUNTS (default 0); atomically freeze their cutoff at onboarding. Existing unflagged accounts remain unflagged; disabled switches retain all data.
- Include enrolled new accounts in the existing bounded importer pool with existing default limits. No provider rates or spending caps were raised.
- Real PostgreSQL qualification: 79 passed in 353.99 seconds pytest, 357.440 seconds wall. Includes synthetic 100-creator registration and simulated 100-account scheduling. This is not evidence of live 100-creator throughput.
- Local preceding suite: 63 passed; scheduler eight passed. These overlap the qualification suite and are not additive.

The first stage (05b0f26e) refused a source mismatch before production changes. The live store.py lacks separate review-branch operator-case/concurrent changes, so store.py was excluded from the deployment entirely. The read_failures.py base was pinned to its exact CRLF byte hash. The final release packet contains the narrow overlay plus immutable base images, with all source and configuration recorded in GitHub.

## Measured Maile checkpoint

This is one account, not the fleet. At 2026-11-02T13:24:11.001808+00:00:

- 91,727 stored window messages across 16,573 fans. Window discovery still incomplete.
- 2,841 incoming window messages processed, zero pending, zero current-window errors. Nine preserved historical errors are outside pending recent work.
- 88,539 baseline sources unchanged and reused; zero baseline sources lost or edited.
- 3,181 new source keys in the comparison snapshot, including live arrivals and the baseline gap. History pages added 1,893 canonical messages.
- 2,714 completed incoming receipts reused; 127 newly completed receipts, including 63 for reused sources.
- 68 new model calls; actual provider-reported cost US$0.0106. Current-pilot estimates, active reservations and unresolved reservations all zero.
- Cumulative new model latency 74.312 seconds. Receipt span 13,077.478 seconds is not total memory-processing time.
- 8,179 total recent provider attempts in the subsequent coverage read, 8,163 new attempts at the earlier comparison snapshot.
- Elapsed from fixed policy start 13,187.372 seconds; import still incomplete. Full-history speedup and fresh onboarding duration remain unmeasured.

## Remaining release gates

The reusable backend is deployed, but rollout remains Maile-only. Automatic new-account enrollment remains 0 on all four processes pending pilot qualification. See NEW-ACCOUNT-ROLLOUT.md for the reviewed switch/cohort sequence; do not imply current activation on the other accounts or on 100 incoming creators.

Recent coverage has not succeeded: discovery is incomplete, 23,897 conversations await coverage, and errors remain. Unverified timestamp ordering requires exhaustive ID traversal/date filtering. The current implementation limits initial AI extraction, but a shorter provider import is not demonstrated. Importer progress continues in the running service; no fixed completion time is promised.

All 25 saved Maile personality revisions are drafts, with no structured approved onboarding found. Accessible Drive search did not recover her sheet. Her biography, boundaries and operating rules need an authoritative supplied source and the existing approval workflow.

The exact dashboard source for image 22be6bc8 / reported commit 3d0ee2e3 still returns GitHub 404. Existing dashboards and the tested backend compatibility protection remain live. The detailed recent-history card cannot safely deploy until the original builder's Git objects or complete matching build context is recovered. Rebuilding from an older branch is excluded.

## Traceability and rollback

Build/qualification SSM: 02cbe2b4-8bd5-4283-90cd-32b93bb3baef (Success).
Promotion/measurement SSM: 2cc84454-dc52-4a89-bf28-c1df5fb6b536 (Success).
Independent health SSM: 8031a749-2837-4e44-bee6-4c6303910f90 (Success).

Host release directory: /var/lib/twin-release/recent-history-20261102-resume/repair-9d6756b20063.
Its deployment.json is the latest manifest. Previous deployment-repair.json remains historical 91c260 evidence.
The exact prior compose and container state, schema-compatible flag-off rollback, build/qualification records and new images are retained there. Original dump and recovered baseline remain preserved. Do not use schema-0106 binaries as rollback. Never delete receipts, memories or imported data or enable replies during rollback.
