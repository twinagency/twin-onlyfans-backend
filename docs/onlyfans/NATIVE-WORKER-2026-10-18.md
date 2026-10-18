# Native worker orchestration — 18 October 2026

This checkpoint connects durable intake, native preparation, the existing common
delivery authority and receipt recovery in a bounded offline worker loop.
Database head stays `0079_native_configuration`; all 0070–0079 SQL snapshots
remain unchanged. No provider or paid-model adapter has been enabled.

## One owner for each phase

`onlyfans_runtime.native_worker.NativeWorker` coordinates the existing `Store`,
`NativeTextPlanner` and `TextAuthority`. It does not introduce another queue,
planner, cost ledger, prompt, media matcher or sender. Existing intake adapters
still write typed events through Store. The loop drains those recorded events;
it does not construct or poll a live provider reader.

Each worker receives an explicit tenant and one to 32 immutable `WorkerAccount`
bindings. Each binding supplies its account locator, its own exact
`SyntheticNativeModel` instance, and an exact account-bound synthetic or owned
loopback transport. There is no URL/session/key/environment factory, arbitrary
model fallback or runtime activation command. Startup and each worker step
require the dedicated worker role. A cached worker refuses a later role switch.
The API process can import the module without loading any legacy writer/model.

A cycle rotates over at most four configured accounts. For each it performs
bounded dispatch-expiry and accepted-receipt repair, drains at most the existing
100-event intake page, examines at most four interrupted native plans, dispatches
at most one queued intent, and considers at most one new planning claim followed
by its one-use dispatch. An account can therefore finish an older queued item
and a fresh item in one cycle if its pacing permits. No transaction or row lock
spans model or HTTP work.

`Store.claim(account_ids=(...))` adds a positive bounded account allowlist to the
existing fair account/fan claim query. Omitted scope retains the previous tenant
claim behavior. A worker cannot claim or block an unregistered account merely
because it belongs to the same tenant. The existing runtime/binding lock orders
scope checks. The configured account window rotates between cycles; separate
processes still compete through PostgreSQL leases and the existing unique native
input and one-use dispatch constraints.

Planning preflight respects enabled/current history, queued work, active unknown
attempts, consumed allowance, existing pacing and the bound transport generation.
The planner/authority recheck their own canonical conditions at their existing
transaction boundaries. A preflight result grants no send rights. Refused or
terminal planning releases only the exact current lease through the Store owner;
newer input/control generations are preserved.

## Prepared-plan and crash recovery

`NativeTextPlanner.recover` has no model parameter and cannot regenerate a reply.
It can queue a completed ready plan while the original planning lease, input,
control, configuration/facts, cost proof, binding and expiry remain valid. The
normal authority performs the final queue/admission/one-use checks. The saved
original claim, body, cost and expiry are not renewed, rebased or rewritten.

Interrupted generating plans become unknown when their original authority is
lost or expired. Known completed ready plans with expired/replaced authority
become stale. Their matching old work is blocked; fresh eligible input is needed
for another invocation. Temporary unresolved-account/history/queue holds may
retain a valid ready plan only within its original deadline. A revoked binding
grants no new queue/send permission. Old accepted receipts may still project
through the existing historical repair boundary after revocation.

Recovery distinguishes five crash points: during model generation, after ready
preparation, after queue commit, after admission and after acceptance commit.
Generating uncertainty retains the original reserved model expense. Ready work
can finish without another model call. Queued work survives without regenerating.
A dispatch abandoned after admission expires to unknown, retains its unit and is
not resent. A saved acceptance repairs the canonical Message/receipt/offer once
without calling transport again. Missing response is never a negative receipt.

## Polling, shutdown and health

`run(stop_event, ...)` accepts an explicit cooperative stop event and an optional
bounded cycle count. Polling is interruptible, bounded to 0.05–5 seconds, and
failure backoff increases to a configured maximum of at most 30 seconds. The
default poll is 0.5 seconds and maximum failure backoff is five seconds. A DB
failure ends that cycle without speculative model/send retry; existing pooled
connections reopen normally after recovery.

Stop is checked between phases. A started model settles its durable usage and
can leave a recoverable ready plan when shutdown is requested. An already
started wire phase finishes its known receipt bookkeeping. No subsequent model
or dispatch phase starts once stop is observed. A hard process death can leave
uncertain evidence and is handled by the persisted invocation/wire markers.
This is cooperative draining, not a process supervisor or a guaranteed hard
wall-clock shutdown deadline over database operations.

`CycleReport.public()` emits `of-worker.v1` and integer counts only. It excludes
account/fan IDs, bodies, prompts, tokens, raw SQL and exception text. It is an
injected operational observation callback, not a new public health route or a
claim that production monitoring is deployed. A callback's own persistence and
supervision remain the host's responsibility. Counts describe per-cycle
observations and can include an idempotently observed result across competing
workers. Do not sum them as unique sends, usage or financial totals; the canonical
intent/receipt and cost ledgers remain the authority.

## Product rehearsal and verification

The existing one-command demo now records a fresh neutral inbound in a separate
synthetic worker account. The actual worker drains it, retains the real
seven-second quiet window, performs one native synthetic invocation, posts once
to an owned local HTTP listener, and projects the accepted canonical reply.
The real API/Next/Chromium flow opens that conversation and checks its sent
receipt, exact model cost and used allowance. Existing Content/Sales/Settings,
authentication, control, conflict, lost-ack and session-loss checks remain in the
same browser acceptance.

```bash
ONLYFANS_TEST_PG_URL=postgresql://test_user@127.0.0.1:5432/postgres \
  .venv/bin/python service/tests/onlyfans_demo.py --dashboard ../twin-dashboard --smoke
```

The demo uses fresh private read/control/configuration credentials, excludes
inherited provider credentials, creates a random owned local database, starts
actual local API/Next/browser processes and cleans its own state on completion.
Required local dependencies are documented in the preceding rehearsal runbook.
No production route can inject this fixture or bypass real login.

Meaningful worker acceptance covers process death at every recorded phase,
competing full worker processes, actual database connection outage/backoff,
round-robin account progress, account/tenant exclusion, role changes, stopped
model/wire phases, unknown/negative HTTP evidence, stale configuration/control,
pricing/pacing, existing-photo/video media and the unchanged canonical financial
semantics. Accepted media creates an open offer, not a purchase; uncertain media
creates no transcript/offer and keeps its unit. Wire price is checked against the
frozen native quote, including native price arithmetic rather than an assumed
sticker price. Current results and non-overlapping counts are in VALIDATION.md.
Previous bounded load figures remain synthetic evidence, not fleet certification.

## Remaining release work

This completes the current offline worker milestone. The private real provider
adapter/session lifecycle, real model preflight, authoritative provider delivery
and payment reconciliation, curated vault packs and full semantic/policy
acceptance, content management writes, authoritative legacy import, external
writer retirement, production supervision/monitoring/fleet soak and staged live
cutover still require their corresponding implementation and acceptance.

No Git commit/push, deployment, real account/session access, paid model call,
provider send, remote database mutation, allowance reset or Telegram runtime
change was performed. Continue the accepted ownership map and original handover;
do not replace native Fastt policy or the shared delivery authority.
