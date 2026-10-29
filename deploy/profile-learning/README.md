# Automatic OnlyFans profiles

Release candidate, not deployed. Production access expired during implementation.
Base repository commit: 0af4132ee90bb1886ec64367f1445e337ba5d14d.
The running release has not been re-established. Compare its runtime patches and
schema before integrating; do not replace the live sender with this old base.

Validation: 35 passing tests (22 evidence validation, 13 SQLite queue/incremental
behavior). Python compilation passed. No live model calls, PostgreSQL integration,
browser acceptance or production backfill were run for this candidate.

## Behavior

The standalone learner enumerates every active OnlyFans binding in its explicit
tenant. It seeds every fan, including silent fans, into durable processing state
and creates missing fan profile records. Discovery proceeds in batches of 500
per account without a total roster limit. History processing advances through
40-message pages, checkpoints atomically, and continues until imported history
is exhausted. Newly imported old messages are discovered by ingestion time.

Every completed fan is checked again after two hours. This is an incremental
scan of stored history, independent of reply activation and OnlyFans session
expiry. A supervised process must remain running. Backfill throughput and budget
limits affect when a large backlog completes; two hours is the refresh interval,
not a guaranteed fleet processing SLA. This code does not import missing provider
history. The existing history reader must keep operating.

The model returns source quotations, validated before persistence. Sources must
have applied ingress under the current binding. Unknown-authorship outgoing
messages become creator account claims in an automatic personality draft. They
are never promoted to verified biography or approved live configuration. Human
reviews and manual profile corrections retain priority. Existing canonical fan
fields and purchase/ownership/spend sources are retained. New observations appear
as Conversation summary in the existing inbox facts view when the read flag is on.

Summaries are dated evidence excerpts, not invented prose. Memory retains up to
six statements per category and 64 overall; all imported pages are examined, but
this is bounded working memory, not an exhaustive biography archive. Sources are
retained in message history and model audit records. Deleted/changed retained
sources are removed on refresh. Edits to already-processed messages that did not
previously yield retained facts need an explicit rescan; ordinary new imports
are picked up automatically.

The account's encrypted model credential and selected registry model are reused.
No cross-account or house credential fallback is introduced. Missing credentials
remain explicit per-account errors. Model calls use the existing daily budget
and audit ledger; expired OnlyFans sessions do not prevent reading stored text.
The budget account-config lock is exclusive to serialize first daily ledger
reservations as well as existing ledger updates.

## Integration and verification still required

1. Restore the operator's AWS login. Inspect current containers, image/source
   hashes, configuration, schema and uncommitted production patches.
2. Integrate only the changed files into that release. Apply schema.sql explicitly
   after examining index size and lock impact. On a busy of_ingress table, build
   the supporting index CONCURRENTLY as a separate migration operation.
3. Run PostgreSQL concurrency, transaction rollback, budget and credential tests
   against an isolated copy. SQLite tests simulate advisory lock functions; they
   do not prove PostgreSQL lock behavior or production performance.
4. Start the learner using the same backend worker image/environment/network,
   explicit ONLYFANS_PROCESS_ROLE=worker and ONLYFANS_WORKER_TENANT. Command:
   `python -m onlyfans_runtime.profile_learning.worker`. Give it a separate
   restart-always supervised service; do not replace the existing reply worker.
5. Set ONLYFANS_PROFILE_LEARNING_ENABLED=1 on the read API only after the table
   and worker are installed. Verify inbox summaries and timestamps in the browser.
6. Inspect each account's saved model credential. Repair missing intended
   configuration through the existing credential owner; never copy a different
   account's key. Verify actual model usage/cost settlement before fleet backfill.
7. Verify fan totals versus processing state, a late historic import, new incoming
   messages, operator locks, source withdrawal, current binding changes, process
   restart and budget exhaustion. A queue entry is not a claim of completed history.

Progress query (private operational use):

```sql
SELECT account_id, count(*) FILTER (WHERE fan_id>0) AS discovered_fans,
 count(*) FILTER (WHERE fan_id>0 AND checked_at IS NOT NULL) AS checked_fans,
 count(*) FILTER (WHERE error_code IS NOT NULL) AS deferred,
 sum(processed_messages) AS processed_messages
FROM of_profile_learning GROUP BY account_id;
```

Rollback: stop the learner and unset the read flag. Retain processing state and
audits. Automatic creator revisions remain drafts. No message sending, activation,
delivery counters, model selection or paid-content rules are changed by the learner.
