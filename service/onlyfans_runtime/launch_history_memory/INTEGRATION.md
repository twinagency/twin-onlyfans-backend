# History recovery and fan memory — integration handoff

## Scope and baseline

Branch: `codex/onlyfans-history-memory-20261023`.
Backend parent: `dbd09b06dace2c24829ded6bc778d7e6d2ded4f0`
(`codex/onlyfans-offer-import`, schema `0089_offer_import`).
Dashboard contract baseline: `927b2d75105f6180c37f2759ed119f89521b979d`.
No dashboard files are changed. No ownership register existed according to the
provided Claude handoff; these new paths were absent at the pinned backend.
No applicable AGENTS.md was found; CLAUDE.md was read. All changes stay in this
directory and `service/tests/test_onlyfans_launch_history_memory*.py`.

This is an **offline contribution for Claude's combined integration/checkpoint**.
It adds no separate approval gate. It is not mounted into the worker or API,
does not change the shared schema head, and proves no live backfill completion.

## Diagnosis verified from the pinned implementation

* `worker.read_step` drains ingress, scans chats, then reads one history page.
  `Store.request_poll` cannot start another scan while account recovery remains
  pending. `Store._mark_current` waits for every recovery cursor. Consequently,
  initial deep history and current-message freshness share a completion gate.
* `native_worker._can_dispatch_campaign` and `TextAuthority._eligibility` hold
  planning/sending on that same account-wide recovery flag. Simply suppressing
  the flag would bypass outage/discovery/quarantine safeguards.
* `Store._apply` increments `input_revision` before classifying history. Old
  imports can invalidate live claims without updating/requeuing live OfWork.
* A malformed history row rejects its whole page. Fair recovery already exists
  in the pinned code; do not reintroduce lowest-ID retry starvation or invent a
  permissive decoder without provider evidence. No new provider trace was taken.
* Native planning selects the latest 100 non-unsent messages. Saved typed Fan
  facts and FanProfile short_bio/bullet_points reach the composer. Existing
  native_memory accepts quotes only from the latest inbound, inside a real,
  cost-settled reply plan. Historical import invokes no such processing.
* OfNativeMemoryChange requires a genuine native plan and source FK. Fake reply
  plans must not be manufactured to backfill historical evidence.
* Existing fan-fact UI omits occupation/preferences and profiles, and labels
  every displayed fact `imported`, including subsequently corrected values.

The supplied offset-40 diagnostic concerns ledger date-window handling, owned
by the ledger chat. This contribution neither changes its parser nor treats
priced outgoing messages as paid. Offer terms remain observations only.

## Implemented behavior

`recent.request_before` and `commit_recent_page` provide an atomic initial
recent/archive cutover. They start at the newest page even if a legacy worker
left an older cursor, accumulate at least 100 observed messages (or explicit
provider exhaustion), and persist short-page progress. Initial messages are
always imported silently. After initial completion, even a previously empty
conversation uses existing live catch-up behavior. Nonzero high-water outage
recovery is never shortened to 100 messages: it must still close its actual gap.

`HistoryRecovery` has independent discovery and per-fan archive cursors, binding
and lease/generation fences, fair retry, a reserved ingress allowance for live
events, bounded page reads, and same-transaction offer observations. It writes
canonical OfIngress/Message through Store with `source=import/history_only=True`.
It yields to waiting/claimed live work on active accounts and rechecks after the
network read. A conflicting immutable message or timestamp quarantines the
offending evidence and holds normal recovery without advancing archive coverage.
Discovery failure does not starve known-fan archive pages. No transaction spans
provider I/O. Reader injection is not permission to run a live diagnostic.

`HistoricalMemory.process` processes at most 100 canonical messages in a short
transaction. Receipts and evidence commit together. A source anti-join catches
late older inserts, edits, unsends and algorithm changes; an ascending cursor
alone would miss late history. `step` fairly selects one pending fan and yields
to an active claim. Crash before commit replays; crash after commit is idempotent.

The default extractor is conservative, deterministic and English-only. It
recognizes explicit first-person declarations for the existing seven fact fields
and abstains on quoted/compound/hypothetical statements, questions and nonadult
age proposals. This is **not general semantic extraction**. No paid model,
unbounded summarizer or legacy nickname/provider writer is invoked.

Retrieval supplements empty canonical fields with the newest valid historical
evidence ordered by source time and ID, not import time. Existing canonical Fan
facts and saved profiles retain precedence. Historical facts do not overwrite
them. Saved profiles remain bounded in the prompt; source-linked excerpts provide
an extractive account of older interactions. There is no claim of a newly written
abstractive biography. Up to eight query terms select up to eight older excerpts;
common words are excluded and term-match count precedes recency. No vector/semantic
recall guarantee is made. Retrieval excludes the current latest-100 window.

Source edits/retractions invalidate old evidence immediately, before reprocessing.
Manual set/clear commands have full-value optimistic concurrency, idempotency,
actor checks and append-only provenance. Clears are durable overrides, so older
imports cannot repopulate them. Existing native updates must honor these overrides
via the integration hook below. No unrelated profile fields are overwritten.

## Exact shared integration changes for Claude

1. **Schema/migration and registration**: allocate the next combined revision
   after unpublished 0089 (do not create a second head or rename Claude's 0089).
   `schema_contract.migration_sql()` returns PostgreSQL CREATE TABLE/index/trigger
   SQL for review; it never executes. Import `recent` before enumerating
   `RecoveryBase.metadata`, because it adds the initial-head checkpoint table.
   Register all tables from `MemoryBase.metadata` and `RecoveryBase.metadata` in
   the shared schema validator, marked with that new revision. They intentionally
   use separate metadata today, so import cannot mutate shared startup readiness.
   Include `EVIDENCE_GUARDS_SQL` append-only triggers for evidence/correction tables.
   Update the shared schema rank/head, frozen snapshot/migration and validation
   tests together. Rehearse populated 0089 upgrade, backup/restore and downgrade
   policy; no live migration has been attempted here.

2. **`worker.py::read_step`**: keep existing drain and chat discovery. Replace
   the history reader's `before_id=recovery.before_id` with
   `request_before(store, tenant_id, recovery, **scope)`. Replace its
   `store.recovery_page(...)` call with
   `commit_recent_page(store, tenant_id, recovery, page.events,
   has_more=page.has_more, observations=page.observations, **scope)`.
   Both replacements are necessary. Preserve the same connection revision at
   claim, read selection and commit. Count only successful committed pages.
   Never pass a client-supplied initial cursor or manufacture completion flags.

3. **`native_worker.py` / shared `onlyfans_worker.py` scheduling**: after recent
   intake, before claiming new replies, allow one `HistoricalMemory(store).step`
   with a bounded limit (default 40). After reactive work, give
   `HistoryRecovery(store).step` a bounded turn using the existing same account
   reader and connection revision. Coordinate its at-most-two reads with offer
   import/content sync and the existing account/provider throttle; do not add an
   independent poller. Recent intake must run even if archive work is retrying.
   Initial recent cutover creates archive cursors atomically; account archive
   discovery is explicitly requested once through `request_account` and resumes.
   Continue seeding newly discovered fans through the shared discovery owner.
   Inactive verified-account imports may run without a model or automation.

4. **`store.py::_apply` revision refinement**: preserve every existing identity,
   conflict, future-timestamp, binding, ownership and control check. Compute
   `historical` before adding Message. Increment the reply `input_revision` if
   the new message is not historical, or if
   `affects_recent_context(session, account, fan_id, event)` is true, evaluated
   before insertion. That predicate exactly uses non-unsent timestamp/ID ordering
   and the 100th-message boundary. Do not reset revisions after commit. Archive
   yielding already protects waiting/claimed work without this shared edit.
   Memory publication and corrections remain separately snapshot-fenced below.

5. **`native_context.py::versions`**: after constructing the current snapshot,
   call `snapshot = enrich_snapshot(session, account_id, fan_id, snapshot)`;
   hash the resulting complete snapshot with the existing canonical SHA256.
   Keep the existing full Fan/Profile/config locks and creator-facts checks.
   This is shared by generation, queue and final egress. It must run at every
   existing versions check, not only at prompt generation. Missing new tables
   are readiness failures; do not silently drop historical memory on DB errors.
   `enrich_snapshot` derives its query from the actual scoped latest inbound.

6. **`native_planning.py::_native_messages`**: keep the existing latest-100 SQL
   and native composer intact. After composition, call
   `augment_prompt(messages, context['historicalMemory'])`. Retain both current
   context/output byte checks. The addon is bounded to 12 KB and explicitly
   classifies excerpts as untrusted historical data, never current input or
   payment/contact/price authority. A combined large context may still hit the
   existing cap; do not trim the required latest 100 messages to conceal it.

7. **`native_memory.py::apply_memory`**: after existing source/lease/cost validation
   and before writes, use
   `proposal = respect_overrides(session, plan.account_id, plan.fan_id, proposal)`.
   This removes only locked memory writes. Preserve reply/sale terms and current
   durable-fact rules. A locked profile field suppresses the paired automatic
   profile update so its counterpart cannot overwrite an operator override.

8. **Shared API mounting, agency middleware and dashboard**: mount the contracts
   below with existing purpose authentication and trusted BFF actor/tenant/creator
   scope. Add the chosen namespace to the existing agency path allowlist; reads
   preserve creator narrowing and mutations require canManage and actor_allowed.
   Map Refused to stable conflict/invalid/scope errors; never serialize raw SQL or
   provider exceptions. Use private, no-store responses. UI navigation and types
   are Claude-owned. Do not expose transaction-internal `snapshot` directly.

## Proposed API/data contracts (not mounted)

Use `/onlyfans/history-memory/v1/accounts/{accountId}` under the existing trusted
creator/account resolver. All fan/message/provider IDs are decimal strings in
JSON; internal modules accept validated positive int64 fan/message IDs.

* `GET /progress` or `/fans/{fanId}/progress`: combine
  `HistoryRecovery.progress` (`of-history-recovery.v1`) with per-fan
  `HistoricalMemory.progress` (`of-history-memory-progress.v1`). Show recent
  recovery requirement, discovery completion/cursor/error, committed pages,
  observed/added messages, completed/discovered/deferred/retrying conversations,
  fan cursor/generation/retry/oldest/completed timestamps, stored/processed/pending
  memory counts and extraction version. Runtime controls, pending/quarantine and
  ledger holds remain separate existing owners. `totalMessages` and
  `percentComplete` are null; do not invent a provider total.
* `GET /fans/{fanId}/memory?query=...`: `HistoricalMemory.read`, query <=2,000
  characters, at most eight excerpts. Returns `of-history-memory.v1`, algorithm,
  revision/version, canonicalDigest, semantic fingerprint, facts with canonical,
  history or operator provenance, source IDs/times/quotes/digests where available,
  saved summaries, override operations/revisions and older interaction excerpts.
  `summaryKind=saved_profile_and_source_excerpts` distinguishes real provenance
  from an invented generated biography. Historical source quotes are evidence
  of what was said, not independent truth verification.
* `POST /fans/{fanId}/corrections`: field, operation (`set` or `clear`), value
  (null for clear), expectedVersion, commandKey, reason. Actor and tenant come
  only from authenticated server context. Native seven fact fields plus
  short_bio/bullet_points; same adult-age/value bounds as current native memory.
  Repeating the identical command returns its committed revision; changed payload
  with the same key conflicts; stale full canonical version conflicts. A clear
  persists until an explicit later set, with older evidence retained for audit.
* `POST /backfill` or `/fans/{fanId}/backfill`: authorized manage operation calls
  request_account/request_fan. It resumes existing checkpoints, never resets or
  force-advances them. Cursor overrides are internal only. A held conflicting
  message requires existing investigation/correction; retry never clears a hold.

Provider/archive completion, canonical ingestion completion and memory processing
completion are different indicators. `storedMemoryComplete=true` means only
currently stored canonical rows match this processing version. Its
`providerHistoryComplete` remains null until joined with archive evidence.
Archive complete means the scheduled range reached has_more=false and, for an
account, discovery/known-fan scanning finished. Recency-offset chat discovery is
mutable, not an immutable provider snapshot. Busy fans can defer archive work;
inaccessible/provider-omitted messages remain outside observed coverage.

## Validation and remaining integration work

Dedicated tests cover >1,200 messages, a unique middle-history fact and interaction,
late imports, latest-100 preservation, real native composer/synthetic model
rehearsal, in-flight correction/retraction invalidation, no historical reply work,
initial/new input cutover, short pages, empty initial history, restart/CAS/replay,
atomic rollback, exact offer observations, quarantine, tenant/fan/agency/revocation,
operator override/clear and append-only evidence. Exact run results are in
`VALIDATION.md`.

The planning test uses test-local adapters for shared hooks. Passing that
rehearsal does not mean the production worker, prompt or UI is wired. Claude must
apply the eight integration changes and run the combined tree's migration,
worker, memory, offer, ledger, fixed-price and access tests. English extraction
and lexical retrieval are intentionally bounded capabilities; general multilingual
semantic summaries/recall remain outside this implementation. No provider call,
live account backfill, activation, fan send, merge or deployment occurred here.
