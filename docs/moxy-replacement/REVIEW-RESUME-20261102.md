# Moxy review checkpoint, 2 November 2026

Status: implementation candidate, unfinished and not release-qualified.

## Exact preserved commits

Backend branch on Work Server: codex/moxy-replacement-20261102.
- Original preserved candidate: 8df66277e4381a860cd8b5b7eef3123ab1c1366b.
- Controls/concurrency: d0e18da4aa49b4a29e7d7fa3a48f41f171716225.
- Campaigns: 567a52d03c3d3f56f5a8fa8b996e3ae217d2121a.
- Durable input constraints/historical upgrades: c67e1fe19e35f04922baeedc33976d4d5b0698c8.
- Onboarding compatibility/reporting: 992e02785e18eb4516207844d62de547388f9c32.
- Current tested server HEAD: 0ea1edbf64d133ab525f6d5a9a62b25374d9086d.

Dashboard companion HEAD: a39e34a524b74bdf3bb2e5571453e56401796864.
Its archive baseline is c5f1915, taken from upstream 0cab2aee9669d57e989e1a26832085308c240c81. This is not the exact live dashboard source. Do not deploy this archive over the running dashboard.

The server's direct git push failed because HTTPS GitHub credentials were unavailable. Publication uses a new review snapshot commit through connected GitHub access, based on d4c0eab2965bcf0a03605606fdb211ce05693634. The original server commit IDs above remain preserved on the server. The published snapshot has a different commit ID; its service delta was checked against the preserved server files. Existing branches are not rewritten. See PUBLICATION.json for the actual published commit.

## Verified results

- Full Moxy fixture suite: 108 passed in 293.55 seconds, before selected history reconciliation.
- History/connection/Moxy integration: 70 passed in 253.34 seconds, after reconciliation. Counts overlap; do not add them.
- Focused earlier run: 34 passed in 150.19 seconds, also overlaps.
- Next HTTP loopback fixture passed. Dashboard type and contract checks passed. Browser fixture passed at widths 1280, 390 and 320.
- Synthetic 100-fan tests: 2 passed in 293.11 seconds. Amelia preparation 66.137 seconds, total 134.741 seconds. Cristina preparation 72.038 seconds, total 138.650 seconds. Each creator ran separately, with four dispatch threads and worker-equivalent pool size eight.
- These load results used simulated model output, simulated provider receipts, plain replies and no approved reply pacing. They do not prove actual-model quality, live provider performance, 50-creator capacity, media sale conversion or earnings.
- Actual-model evaluation: not run. No live-provider/customer send performed.

Evidence includes successful runs and failed exploratory load runs. The fixture setup was corrected for missing runtime schema, duplicate fake provider IDs and a two-connection fixture pool. Production receipt protections were retained.

## Outstanding work

1. Broader provider 401/429/recovery and simultaneous multi-creator qualification. Confirmed provider rejection currently retires queued Moxy events as stale; safe resumption needs qualification.
2. Complete source-required budget/timing, paid-service and custom fulfilment workflows and end-to-end asset-description import/change checks.
3. More contextual held-out examples and bounded actual-model evaluations under an explicit recorded budget.
4. Locate and bind approved actual assets, prices, complete creator sequences/access levels, exact purchase links and current ownership. Do not infer links from similar times/prices.
5. Verify operational labels, graveyard-shift definition, remedy/fulfilment owners and external automation coordination.
6. Recover exact live dashboard source before frontend release integration.

This package excludes credentials, tokens, raw fan-message exports, original private biography PDFs, customer databases and screenshots. Configuration references creator rules; test conversations and provider receipts are fixtures. Historical aggregate review reports and raw training exports are not included.

## Resume instructions

Continue the original START_HERE.md requirements included here. Do not restart the build or treat the repository default branch as the latest integration base.

Work Server backend: /srv/projects/moxy-engine. Venv: /srv/projects/moxy-engine-env. Dashboard: /srv/projects/moxy-dashboard. Source requirements: /srv/projects/moxy-source.

Before edits, inspect HEAD/status on both repos and preserve subsequent changes. The dashboard artifacts directory is generated/untracked. Use dedicated review branches. Preserve onboarding/history and concurrent reply work; in particular do not overwrite the Moxy refuse_legacy fence in twin_bridge/coordination.py.

Tests must run on the Work Server, using its isolated PostgreSQL fixture, never production or the Surface. Existing wrapper: /srv/projects/moxy-resume-test-runner.py. It reads the task container's disposable database credentials internally. Invoke it with a log name and pytest module paths; never print those credentials. Existing logs are /srv/projects/moxy-*.log and .exit.json. Browser fixtures require LD_LIBRARY_PATH=/srv/projects/moxy-browser-libs/extract/usr/lib/x86_64-linux-gnu.

This is a delta source package, not a complete standalone checkout. The backend directory contains current files changed from the published integration ancestor plus Moxy configuration/tests; obtain the published review branch for a complete base. The dashboard-companion directory is a delta against its archive baseline. SHA256SUMS.json records exact included bytes.

No merge, deployment, activation, production setting change, purchase or customer message is authorised. Preview must not send or advance live purchase state. Disabling Moxy must preserve purchases and obligations and must not enable a legacy writer automatically.
