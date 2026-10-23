# Combined OnlyFans release and live acceptance

The combined backend/dashboard candidate is the release unit. Use existing direct
user authorization for each precise live action; finish all independent coding,
testing and release preparation before raising one concrete missing-authorization
list. Do not repeat the completed combined checkpoint.

## Candidate and evidence

- Start from `codex/onlyfans-final-integration-20261023` in both repositories,
  reconcile newer work, and record final full SHAs and complete merge ancestry.
- The single migration sequence is `0089_offer_import` → `0090_launch` →
  `0091_launch_controls`. Never stamp an old schema over newer objects or roll
  back to an incompatible binary after a bump.
- Update `docs/onlyfans/ACCEPTANCE-DASHBOARD-SHA` to the final pushed dashboard SHA
  before accepting the backend release. The branch name is not a source pin.
- Keep exact commands, environment versions, collected node identities, JUnit,
  browser evidence and per-command summaries. Runs overlap; never sum their test
  counts into a unique total without deduplicating test identities.
- Build deployable artifacts from those tested SHAs. The workflow records the
  candidate image ID; record the registry digest of the artifact actually
  promoted separately. An earlier feature image is not this release's digest.

## Required acceptance: zero exemptions

Every required command must collect nonempty tests and finish with zero failures,
errors, skips and deselections. A cancelled run or unavailable paired checkout is
incomplete. The previous two-failure exception is removed.

Install the backend pins, the exact paired dashboard lockfile and its browser.
Use only disposable PostgreSQL, with compatible `pg_dump`/`pg_restore`. Supply
`ONLYFANS_TEST_PG_URL` and a separate
`ONLYFANS_PERSONALITY_TEST_DATABASE_URL`; missing PostgreSQL proofs must fail.
Set `ONLYFANS_TEST_PG_BIN` to the absolute directory containing matching,
executable `pg_dump` and `pg_restore` clients. Install the pinned Python
requirements into a self-contained interpreter: the required isolated-child
proof must not rely on user-site packages.
Set `PREVIEW_CHROME` if using an explicitly installed supported browser.

Run from a clean recorded candidate:

```bash
python scripts/onlyfans-release-acceptance.py \
  --suite combined \
  --dashboard-dir /absolute/path/to/paired-dashboard \
  --dashboard-sha <full-dashboard-sha> \
  --backend-sha <full-backend-sha> \
  --evidence-dir /absolute/path/to/combined-evidence

bash scripts/check-onlyfans-offer-import-integration.sh \
  --dashboard-dir /absolute/path/to/paired-dashboard

python scripts/run-service-tests.py
```

The combined manifest includes all native pytest/unittest modules, including
launch, source/content regressions, controls, isolation, populated upgrades,
recovery and actual HTTP/browser rehearsal. The two historical script modules
run in separate interpreters. Collection and execution identities must match.
The commerce workflow uses the declared commerce manifest; it does not replace
combined acceptance or repository CI. The repository's default service runner
keeps its existing separate-interpreter checks; the canonical combined manifest
supplies every native PostgreSQL proof, so those modules need not run twice.
Required repository dashboard type checks,
production builds and ordinary CI remain required.

The actual rehearsal starts the real dashboard server and combined backend on
owned synthetic data, exercises operator flows, and proves cleanup after both
success and frontend startup failure. Browser fixtures remain additional UI
regressions and never substitute for this rehearsal or real-provider evidence.

## Paired source in hosted CI

Both workflows use the committed dashboard SHA. Automatic runs fail explicitly
when neither paired-source capability is supplied; they do not use another
branch or silently skip browser acceptance.

The manual dispatch accepts `dashboard_ref`, `dashboard_archive_url` and
`dashboard_archive_sha256`. An authorized release owner prepares `git archive`
from the exact pushed dashboard commit, uploads it through the existing encrypted
release-source S3 process, records its SHA256, and passes a short-lived signed
HTTPS URL directly to dispatch. Do not print the signed URL, keys or credentials.
The verifier accepts only Amazon S3 HTTPS, bounds compressed/unpacked size and
file count, rejects traversal/links/duplicate entries, checks the archive digest
and git-archive commit proof, and records per-file source hashes. Acceptance
rechecks those hashes before testing. Rooted source must include the lockfile
and real integration harness.

An explicitly provisioned repository-scoped Contents-read credential may instead
be supplied as `ONLYFANS_DASHBOARD_READ_TOKEN`; no such credential is assumed.
No credential is extracted or transferred from another device. Source capability
and deployment authority are separate.

## Before maintenance

1. Retrieve authoritative current API/worker versions, schema, connection/binding
   and expiry, unresolved send/upload intents, account/conversation flags,
   catalogue state, ledger window/hold/cursor, archive/memory progress, worker
   health, model/credential/budget and delivery policy. Historical observations
   are not current evidence.
2. Confirm full combined ancestry reaches intended deployment branches and all
   required checks pass on the candidate pair. Preserve ambiguous evidence.
3. Take the established encrypted backup; prove compatible-client restore and
   independent quarantine on a disposable database. Record one release owner/lock.
   Use the backup and restore tools matching the currently deployed schema for
   this pre-upgrade proof, with compatible PostgreSQL clients. The final tool
   checks its own schema head and cannot back up an older live schema or restore
   an older-schema archive. `Dockerfile.onlyfans` does not include `pg_dump` or
   `pg_restore`; establish the existing external ops runner before maintenance.
4. Keep outgoing authority and new launch flags off throughout migration.

## Maintenance deployment

Old API/worker binaries reject the new schema. Update those two services together;
leave unrelated containers and services alone.

1. Stop `twin-onlyfans-api-1` and `twin-onlyfans-worker-1` cleanly. Do not replay
   unknown transport outcomes or erase upload/send evidence.
2. Run the established migration once, then its schema check:
   `python -m service.db.schema upgrade --url-env ONLYFANS_MIGRATION_URL` and
   `python -m service.db.schema check --url-env ONLYFANS_MIGRATION_URL`.
3. Start the matching tested artifacts. Verify health, schema revision, immutable
   artifact identity, worker health and unchanged authority/permissions. Deploy
   the paired dashboard artifact and record times and digests.
   The repository compose is build-based. Preserve the observed deployment's
   environment, mounts, networks and database, and use an image override pinning
   API, worker and migration service to the same verified registry digest.
   Start only the affected services with `--no-build --no-deps`; a source build
   or mutable tag is not proof of the accepted artifact. The legacy generic
   deployment scripts target other services and are not this release procedure.
4. Run authorized durable inventory/source, ledger recovery and history/memory
   catch-up. Observe actual completion/errors and recent-message responsiveness;
   a queued command is not completed recovery.
5. Review disabled catalogue drafts/source terms, approved personality,
   credentials/budgets, delivery limits and intended conversation settings.
   Enable only explicitly authorized usable items/capabilities.

## Controlled live acceptance

Agree precise account, recipient, prices/spending limits, upload/carrier scope
and observation duration through existing direct authorization. Never invent
approval from this runbook. Perform authorized checks for:

- Existing-fan and eligible new-fan inbound processing, memory-informed reply,
  approved personality and model readiness.
- Proactive offer/follow-up; exact fixed price, attachment order and approved
  previews; verified test purchase, refunds and ownership exclusion.
- CRM upload/import without duplicates, including conservative unknown-write
  reconciliation and permitted carrier cleanup side effects.
- Takeover, no-contact, account pause/emergency stop and explicit resume;
  reconnect/restart without duplicate dispatch or stale queue release.
- Real paid-post continuation and historical edit coverage using the observed
  provider contract, with honest partial/unknown coverage.

Enable autonomous handling only for the authorized account/conversations and
capabilities. Record actual processing, queue health, ledger freshness, model
spend, stop-control behavior and observation duration. Provider history totals
remain unknown unless proven; stored-message coverage is a separate measure.

## Forward fix and final labels

After a schema bump, stop affected automation/sync, retain evidence and deploy a
compatible forward correction. Do not start an old binary that rejects the
schema. Emergency backup restore requires independent quarantine and established
reconciliation; it is not ordinary feature rollback.

Report separate evidence-backed labels: **code complete**, **integrated and
accepted**, **deployed**, **live verified**, **autonomous handling enabled**.
For each give the exact scope and outstanding blocker. Merged branches, feature
tests, fixture screens, queued recovery and permission to activate cannot prove
the later labels.
