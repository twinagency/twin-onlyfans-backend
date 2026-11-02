# Onboarding handoff reconciliation and deployment plan

No deployment, restart, rollback or production data change was performed during this reconciliation. Live access is blocked: the Desktop Commander control laptop was listed offline. Current running images and repair staging cannot be independently reread. The last verified deployed application remains 9870b64889833b16a36397c8ffa45dc7e236cea3; the supplied handoff confirms the existing importer was restarted at approximately 19:59 Brisbane under both release locks after a database lock timeout. That operational restart did not fix its error path.

## Overlap decisions

- Latest onboarding branch inspected: 72ca4a47fc32b17f606bf0dc7bf0d4d7a3ae7c98. Reporting repair c8216d33ef409be5bc99aed0f654b1ccfe3023b3 is already present. Its live deployment remains unverified.
- Standalone 91a8325b1963b0b134d81d7008bc366f93cf51ef was compared, not cherry-picked. Docker stdin attachment and JSON validation already exist. Its useful remaining safeguards were integrated: exclusive snapshot creation and validation of observedAt/phaseProgress in addition to the existing measurement keys. Its independent Docker reproduction is handoff evidence, not a test rerun here.
- Concurrent-reply branch advanced from cfab868e2c87706ddce4ff6cfb2bbeffd76c720f to 766a7992908e5706d024b90e083d6a260f93a8c5. The delta contains two service-side test/tool files plus final-repair documentation/evidence, with no production conversation-engine change. Both service patches apply cleanly. The final-repair documentation subtree had no onboarding edits, so the exact upstream subtree is retained together with the upstream parent in the reconciliation commit.
- Moxy code, UI and configuration are untouched. Future deployment must reread current images/configuration and refuse changed bases rather than overwrite a later Moxy or reply release.

## New importer fix

Both startup and periodic cohort refresh now run within the coordinator error boundary. Known transient database failures (lock/statement timeouts, serialization/deadlock errors and invalidated/disconnected sessions) defer refresh with 1, 2, 4 ... 60 second bounded backoff. Failed refresh discards the cohort for new admissions until credentials refresh successfully; already admitted reads retain durable generation/provider fences. Unclassified configuration/schema failures still fail visibly. Logs contain error types and retry delays, never SQL, credentials or provider responses.

A transient discovery-seeding database failure also defers only that account rather than terminating the pool. Other accounts may progress. Shutdown remains interruptible. No provider rate, concurrency, model cap, cursor, cutoff or reply setting is changed. No new model/provider requests were made by the tests.

## Verification

- Combined history, receipt measurement, scheduler and connection PostgreSQL suite: 59 passed, 93.63 seconds pytest / 96.512 seconds wall.
- Separate real PostgreSQL runtime-row lock contention: 1 passed, 5.65 seconds pytest / 6.827 seconds wall. Confirms SQLSTATE 55P03 is retryable and the failed transaction rolls back, allowing subsequent locking.
- The final scheduler-only rerun covers startup retry, periodic stale-cohort suspension, per-account seed deferral, unknown error propagation, shutdown and existing bounded fan concurrency.
- The upstream native-pilot service file is a standalone test program; passing it to pytest does not execute its 800 checks. Those accepted upstream results remain attributed to the concurrent-reply qualification. Only syntax compilation was repeated for its retained test/tool changes here.
- Exact live-image qualification, baseline reconstruction, complete import and final cost/time comparison remain unverified. Earlier deployed 66-check and reporting 22-check results are separate and must not be added as a distinct-test count.

## Plan before any next deployment

1. Recover read-only control access. Reread the onboarding/reply heads, current images/labels, compose hash, schema, Maile disabled replies, fixed cutoff, dashboards, job status and any source-c821 staging artifacts. Inspect existing SSM commands before retrying staging. Do not rerun initial promotion.
2. Build one combined reporting/importer candidate from the exact current approved immutable bases. The committed packet now includes history_progress.py, history_pilot_measurement.py and parallel_history.py plus regression tests. Hash mismatch requires reconciliation, not replacement. Qualify on isolated PostgreSQL; reconstruct baseline only in the isolated private backup copy with explicit timing bounds.
3. Report candidate commit/images, reconciliation evidence, qualification results and baseline limitations before promotion. No deployment is performed by this checkpoint. Keep both release locks and compare-and-swap checks; preserve all current valid Moxy/reply overlays, frozen cutoff, dashboard images, capacity, budget and disabled replies.
4. After a traceable promotion, run the committed aggregate measurement tool. Report reused archive/receipts, newly saved sources, new completed receipts, provider attempts, provider-reported model charges, estimates and unresolved reservations separately. Label reconstruction gaps, downtime and concurrent live arrivals. Complete recent-window coverage before claiming readiness or end-to-end time. Do not claim a 90-minute result or a measured saving from this partial pilot.

The full recent-history dashboard card still needs the exact source/build context for running OF image 22be6bc8, reported source 3d0ee2e3. Provider timestamp ordering remains unverified, so exhaustive older-page traversal may still be required. These remain explicit release limitations.
