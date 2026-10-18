# Offline durable foundation checkpoint — 18 October 2026

This records the prior checkpoint. The next offline slice, current 0072 head,
review repairs and delivery tests are in
[TEXT-AUTHORITY-2026-10-18.md](TEXT-AUTHORITY-2026-10-18.md).

This implements an **offline milestone 2 slice**, with an authenticated dashboard
read-path rehearsal from milestone 5. It does not close the complete milestone 2
release gate. No production database, account, provider, model, deployment or
Telegram migration was changed. No provider send adapter or planner is enabled.

## Source boundary

| Repository | Published branch | Exact base |
|---|---|---|
| `jesse98/twin-onlyfans-backend` | `codex/onlyfans-of-b1` | `b3a7c0015b3691c34de0bd1c059795f924f518ad` |
| `jesse98/twin-dashboard` | `codex/onlyfans-dashboard-foundation` | `a6539d2ca9f2ad3932cc02444a6b99e9140c5c9a` |

Source files were recovered and verified against GitHub blob identities. The
checkpoint supplies patches against those bases and full copies of changed
files. These changes are not a commit or a pushed branch. The separate dirty
Telegram/native migration, including its 0065–0067 work, was left untouched.
Do not combine its Alembic graph with this one without an agreed integration
checkpoint and populated-database rehearsal.

The existing `OF-B1-READ-SERVICE.md` is a historical record. Its v1 contract,
Boolean-default shim, head revision and freshness defaults are superseded below.

## Implemented behavior

* Explicit PostgreSQL bootstrap and supported populated upgrades; startup and
  authenticated readiness inspect the schema without creating, repairing or
  stamping tables. Boolean defaults are fixed in source and historical DDL.
* Dedicated `onlyfans_app` process with scoped bearer/version gates and private
  no-store responses. It does not import the legacy server's administrative
  routes, caches, streams or automation workers. Starting that legacy server
  with an OnlyFans process role is refused. Nested secret files are excluded
  from the Docker build context.
* Durable, account-scoped ingress deduplicates the same semantic event across
  WebSocket/REST/catch-up/import sources. Applying history, control prerequisites,
  pending counts and conversation work is transactional. Conflicting immutable
  message identities are quarantined without replacing canonical history.
* Bounded intake (1,000 pending events per account), 100-row pages and bounded
  database pools. Backpressure records a recovery requirement before returning
  without acknowledgment. Provider reads occur outside database transactions.
* Persisted chat-discovery and message-recovery cursors, ownership tokens and
  generations survive restart. Recovery walks beyond the newest 100 messages,
  includes known fans omitted from a chat listing, and completes its fixed
  high-water window before advancing. A repeated recovery request schedules
  another pass. Idle draining cannot renew a completed sync timestamp.
* Per-conversation work coalesces bursts (7-second debounce, 30-second maximum
  admission delay), uses account/fan fairness and PostgreSQL `SKIP LOCKED`, and
  rejects stale owners after lease expiry, new input, control changes or binding
  revocation. A claim permits planning only; it is not permission to send.
* Control transitions use expected revisions, idempotency keys and audit rows.
  Resuming `no_contact` requires a recorded consent reference. Unknown delivery
  evidence and takeover holds remain blocking. Missing control starts paused;
  imported/pre-activation history cannot create reply work. Store methods are
  internal interfaces, not authenticated public command endpoints.

## Database operations

Install runtime dependencies with `pip install -r requirements.txt`. Use an
explicit PostgreSQL URL in `ONLYFANS_MIGRATION_URL`; do not use a live target
until the remaining release gates and a reviewed backup/import rehearsal pass.
From the backend repository:

```sh
python -m service.db.schema bootstrap
python -m service.db.schema check
```

`bootstrap` only accepts an empty schema. It creates the frozen canonical 0070
snapshot, stamps that baseline, then migrates to `0071_onlyfans_foundation` in
one transaction. It never overlays an existing schema. For a populated source,
run `python -m service.db.schema upgrade` instead. Supported declared revisions
are 0064, 0070 and 0071 **only when their tables, columns, keys, indexes, foreign
keys and named constraints pass structural checks**. Unknown, partial or
self-healed legacy shapes require a separately validated import. Type-affinity
and named-constraint checks are not a proof of arbitrary historical schema
equivalence; do not stamp a database to make it pass.

The frozen snapshot is the corrected published ORM baseline, not an invented
new owner for native configuration/media. The 0071 migration is forward-only:
downgrade refuses to discard ingress, control and recovery evidence. Backout
means stopping the new processes with sending disabled and retaining the
database; restore/import remains an unfulfilled production release gate.

After explicit migration, the read-only process starts with:

```sh
ONLYFANS_PROCESS_ROLE=api python -m uvicorn onlyfans_app:app --app-dir service --host 127.0.0.1 --port 8018
```

Provide `DATABASE_URL`, `ONLYFANS_API_TOKEN` and `ONLYFANS_API_TENANT` explicitly.
The role does not load `.env` or activate an account. `/healthz` is liveness;
`/onlyfans/api/v1/ready` requires the same bearer and version header as reads and
checks actual schema readiness. No standalone production worker launcher or
live session/transport adapter is included.

## Dashboard contract v2

Both repositories now require `X-OnlyFans-Schema-Version: of-preview.v2`.
The backend URL path remains `/onlyfans/api/v1`; the dashboard's base URL is the
origin (or proxy mount), with that service path appended by its client. Deploy
both contract halves together when a future release is approved; v1 is refused.

`freshness.asOf` / `syncFreshness.asOf` is the last **complete durable sync**, or
null while missing/incomplete. Reads and message activity cannot make it fresh.
The frontend propagates stale state on successful account, list and detail
reads, including unknown timestamps. Connection health stays disconnected
without verified lifecycle evidence, or expired when the stored session is
known dead. Account sending defaults paused. Message retraction is displayed
as `retracted` while retaining receipt evidence. The session parser rejects
malformed signatures and extra token segments before decoding.

## Reproduce offline acceptance and the demo

Use a disposable local/CI PostgreSQL 16 server with permission to create/drop
test databases. Tests create uniquely named databases and remove them. Set
`ONLYFANS_TEST_PG_URL` explicitly. No provider or model credentials are needed.

```sh
pip install -r requirements-onlyfans-test.txt
# In the sibling twin-dashboard checkout:
npm ci
npm ci --prefix preview/onlyfans-integration
npx --prefix preview/onlyfans-integration playwright install chromium
# Back in twin-onlyfans-backend:
python service/tests/test_onlyfans_read.py
python service/tests/test_onlyfans_migration_pg.py
python -m pytest service/tests/test_onlyfans_foundation_pg.py service/tests/test_onlyfans_http_pg.py -q
python service/tests/onlyfans_demo.py --dashboard ../twin-dashboard --port 3018
```

`ONLYFANS_TEST_DASHBOARD` overrides the sibling checkout used by the HTTP test.
The demo requires a loopback PostgreSQL host, creates its own database and
prints a temporary loopback login URL. The isolated Next app imports the real
pages, session checks, identity checks and read handlers. Its synthetic identity
HTTP server replaces the external Supabase service; production auth functions
are not bypassed. The local login route exists only in this harness, requires a
random token and is absent from the production app. Ctrl-C/SIGTERM stops both
servers and drops the demo database. All sample data is synthetic.

Observed acceptance is recorded in the checkpoint's validation report and logs:
real PostgreSQL restart/replay, populated upgrades, multi-process competition,
cursor recovery, controls, revocation, bounded overload, HTTP authorization and
the actual browser-rendered inbox. The browser also checks that losing the
session removes private history and attempts no external network request.

## Remaining gates

1. Complete the exhaustive sender/mutable-policy register and source-to-target
   ownership/data map from milestone 1; accept the shared native checkpoint.
2. Finish milestone 2's production migration/import and role cutover, connection
   lifecycle, provider adapter and deployment checks. Existing legacy deployments
   are not secured merely by adding this unused dedicated entry point. Existing
   legacy password/admin/session paths have not all been converted or retired.
3. Milestone 3 must introduce one delivery authority, uncertain-outcome handling,
   caps/takeover/adapter enforcement and refusal of every unconverted sender.
4. Native planner/commerce extraction follows that boundary. No native owner,
   live provider integration, delivery authority, purchase ledger, production
   capacity claim or live acceptance is delivered by this checkpoint.

The full milestone 2 gate and independent code review remain open. Do not
activate a cohort or interpret offline tests as evidence of provider behavior.
