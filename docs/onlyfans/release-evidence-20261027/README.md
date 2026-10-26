# Controlled release evidence — 27 October 2026

The pinned backend and dashboard passed both native suites and all six acceptance shards. Complete manifest coverage was then verified locally using the pinned checker with Windows path normalization: 2182 pytest cases, zero failures, errors, skips or deselections, plus 2 required isolated scripts. See [the exact paired run](https://github.com/twinagency/twin-onlyfans-backend/actions/runs/40364306652) and aggregate-summary.json. GitHub refused to start its aggregate job because of account billing/spending limits, so the hosted workflow remains red. All eight test jobs succeeded; the complete six-artifact aggregate check succeeded locally. The included adapter normalizes Windows path separators before manifest exclusion/sharding and retains every source, collection, JUnit, duplicate and isolated-script assertion. This evidence does not approve a production release.

## Migration preflight

Before migration, stop worker/sender admission, verify backup and schema-compatible rollback, confirm current deployed fingerprints and run preupgrade-statistics.sql against the explicitly selected target. Then use the existing lifecycle CLI: `PYTHONPATH=service python -m db.schema upgrade` with ONLYFANS_MIGRATION_URL supplied securely, followed by `check`. Its existing 30-second statement and 5-second lock limits remain.

The disposable local PostgreSQL 16 rehearsal populated 750,000 synthetic messages and upgraded in 12.988 seconds after this preflight. Thirty discovery samples measured p95 1.186 ms and used the sparse partial index; runtime authority was preserved and no financial rows or send intents were created. These are isolated single-process measurements. Without refreshed statistics, the first cloud backfill stalled beyond 22 minutes and was cancelled. Do not omit the preflight. It refreshes planning statistics without changing fan history or sending authority. The cloud retry could not start because GitHub reported account billing/spending limits; no billing settings were changed.

## Ordered dashboard cutover

Apply the additive dashboard session SQL before deploying the paired dashboard. Inventory and explicitly coordinate legacy HMAC admin-cookie cutover; preserve existing owner_ and agency sessions. Do not enable the compatibility flag and call it individually revocable. Keep runtime sends disabled during private verification. Do not downgrade by deleting migration evidence or reopening old writers against the new schema.

## Review boundaries

The implementation and browser evidence are described in ../COMPLETION-REVIEW-2026-10-27.md. That document predates this final result; use this supplement and release-manifest.json for final counts, exact sources and migration preflight.

Independent Astra review and controlled provider/model verification remain required before production approval. Standalone creator first connection requires a prepared fixed network; the UI now refuses an unprepared direct-browser start. NativeEngine remains serial, with no 250-creator or real provider-latency capacity claim. The completed browser QA used synthetic identity and transport; its server was stopped after verification.

Production was not deployed or migrated. The latest read-only API/worker comparison retained all 18 checked source fingerprints in each service; refresh deployment/configuration/schema fingerprints again immediately before any controlled release.
