# OnlyFans profiles with dated memory

Release candidate integrated with the API currently observed on 29 October 2026.
Not deployed, backfilled or accepted in the browser. The real-history model test
was blocked by automatic approval review because it sends private OnlyFans text
to an external model destination. Do not start the learner until that is resolved.

## Established running release

Repository source hashes match the changed files read from running containers:

| Component | Source revision | Image |
| --- | --- | --- |
| API | b338c0cfa6d408a5f3729b85e549fdc1743ec9a4 | e5cd643dd613 |
| FASTT sender | f234c641a0afd68b3314ad428284fcc24c96f1c2 | 80341b333fb1 |
| Sync worker, initial observation | 1dcb9846fe1ebee6dc4fe389fa6038ce1313774f | e45c4d6ff228 |
| Sync worker, concurrent update preserved | label still 1dcb9846 | e02594de92cf |
| Twin dashboard | f656663d44a0cf3c762b306a252c89e1b4a4e2e1 | 3e7aff70aa8d |
| FLIRT dashboard | 9ba775278fabf62177197a906f149b3896eb18c4 | bb4e7f7c5e53 |

The database is at `0098_ledger_exclusion`. The profile tables do not yet exist.
Five connected accounts have 65,911 stored fans. These are discovery counts,
not claims that profiles were extracted. No message text was exported to S3.

The staging guard caught the concurrent sync-worker image change before any
replacement. That worker remains intact. The API derivative image
`twin-onlyfans:profiles-api-20261029` was built on the exact API image. It is a
staged image only, not a running deployment. Other service overlays require their
own fresh image/hash checks. Reuse the API derivative for the independent learner
with the existing worker environment; do not replace the sync worker.

`runtime-overlays.json` records narrow, hash-checked changes for the separately
observed API/worker/sender sources. It also contains the common additive modules
and tests. Never deploy the original PR base wholesale. The PR now carries the
integrated API source and preserves its newer permissions, financial views and
sync-related code. Other containers must retain their own source bases.

## Implementation

* Persistent facts, changing circumstances, dated events and outstanding requests
  have separate lifecycles. Observations and historical evidence live outside the
  compact materialized memory and 250-word profile summary.
* Exact fan quotes, preceding-question references, source dates, confirmation
  dates, account/tenant/binding scope and manual protection are retained. Creator
  account claims remain reviewable drafts; approved backend facts remain distinct.
* Relative dates use the original message timestamp and known IANA timezone.
  Imprecise dates remain ranges. Ordinary events leave active context 14 days
  after the end; a passed plan never proves attendance. Cancellation, postponement
  and unclear contradictions retain their uncertainty and history.
* Birth dates calculate age on demand. Age-only statements retain the statement
  date and become ranges when current age is uncertain. Jobs and locations are
  flagged stale after 90 days without confirmation. Unfulfilled requests do not
  time out. No automatic reconfirmation messages are sent.
* Relevance retrieval supplies up to eight memories, at most ten, inside a hard
  6,000-byte context budget. Dates are evaluated at reply preparation. The inbox,
  FASTT hook and native planner share the projection. Undated automatic biography
  fragments cannot reintroduce expired events. Operator overrides remain primary.
* Long messages use lazy overlapping chunks and persistent per-chunk checkpoints.
  Edited, withdrawn and context-question changes invalidate supporting evidence;
  edits to formerly fact-free messages are discovered too. A message is complete
  only when every chunk has committed.
* Every stored fan is discovered in repeated 500-row batches, including silent
  fans. Leased jobs, account scheduling, retries and 1–4 bounded concurrent calls
  are independent of reply activation and OnlyFans session expiry. Triggers mark
  changed messages, complete profiles refresh every two hours, and daily sweeps
  re-evaluate cold records. Queue lag retains the original request time through
  retries, rather than hiding backlog behind a later retry date.
* Subscription, ownership and spend remain canonical OnlyFans data. This feature
  does not change activation, fan permissions, delivery allowances or sales rules.

## Verified offline

The integrated current API release passed 79 focused tests, including 14 real
PostgreSQL tests. A Windows test initially lacked IANA timezone data; installing
`tzdata` corrected the environment and all 29 temporal tests passed. Another
64 existing history/personality/role tests and 26 native prompt checks passed.
Compilation and patch whitespace checks passed.

PostgreSQL coverage includes real leases and concurrent claims, atomic rollback,
first daily budget reservation races/caps, restart recovery, long-message chunk
checkpoints, edited and withdrawn evidence, manual corrections and tenant scope,
expired-event retrieval, a 1,001-fan discovered roster, and the actual native
prompt and FASTT hook. SQLite additionally processes an entire 505-fan roster.
An encrypted synthetic credential plus mocked HTTPS verifies analysis while the
OnlyFans session is expired and reply activation is off. No fake fan input or
real provider message was sent.

## Outstanding real-history permission and configuration

Read-only inspection found:

| Creator | Saved selection | Configured matching credential | Daily cap |
| --- | --- | --- | --- |
| Nicky Baby | openrouter/deepseek-v4.1-flash | yes | 1,000 US cents |
| naomii | openrouter/deepseek-v4.1-flash | yes | 1,000 US cents |
| Maile | openrouter/deepseek-v4.1-flash | yes | 100 US cents |
| Alva Jay Free | configuration needs owner review | not established | not established |
| Katie Price | configuration needs owner review | not established | not established |

The selected destination is `https://openrouter.ai/api/v1/chat/completions`.
The blocked proposal was a bounded real-history sample, followed by ordinary
ongoing learning under each account's own saved model credential and budget.
No key is printed, copied between accounts, replaced or supplied as a fallback.
Alva/Katie require repair through their existing account configuration owner.

## Rollout after the block is resolved

1. Re-establish all image/source hashes, schema and runtime patches; acquire the
   existing native release lock for cutover. Preserve the concurrent sync fix.
2. Run `validate_history.py` in the staged API-derived image with the existing
   worker environment, explicit worker role and tenant `twin-internal`. It reads
   at most eight stored messages for one selected fan per account, sends no fan
   input, and records only ordinary model-budget audits. Inspect the returned
   facts/quotes and source dates before proceeding. Missing account configuration
   is a deferred failure, never permission to borrow another credential.
3. Back up and apply `schema.sql`; if V1 was partially installed, apply
   `upgrade-v1-postgresql.sql` before V2. Build the ingress index concurrently
   separately on a busy database. Apply `notifications-postgresql.sql` only after
   the new tables exist. No sender/control or payment table is rewritten.
4. Add a separate supervised learner using the staged current API image with the
   existing worker environment, `ONLYFANS_PROCESS_ROLE=worker`, explicit tenant,
   `PYTHONPATH=/app/service`, and `ONLYFANS_PROFILE_CONCURRENCY=2`. Command:
   `python -m onlyfans_runtime.profile_learning.worker`. Keep the sync worker.
5. Deploy the API and sender as derivatives of their individually verified images.
   Preserve exact existing env/volumes/secrets and rollback images. Set
   `ONLYFANS_PROFILE_LEARNING_ENABLED=1` only on installed read/reply services.
   Stop the sender before its replacement and preserve every activation/counter.
6. Verify creator draft status and fan coverage via the existing inbox and
   `/history-memory/profiles/progress`; inspect one fan through
   `/history-memory/fans/{id}/profile`. History is paginated under `/profile/history`.
   Versioned set/clear/protect/unprotect commands use existing managing grants at
   `/profile/memories/{key}/corrections`. New structured editing controls are API
   capabilities; a dedicated visual editor has not been added to the dashboard.
7. Verify actual browser display and captured generated context on authorised
   existing history, without sending fabricated messages. Measure actual queue
   lag and counts until all available history is processed. Two hours is the
   refresh schedule, not a proven fleet completion SLA. Continue through older
   history as the importer supplies it, and report incomplete provider import.

Rollback: stop the learner and disable the read/reply feature flag; restore only
changed API/sender images, preserving newer sync work, credentials, counters,
profile evidence and model audits. Disable the notification trigger if rolling
back ingestion overhead. Do not drop the stored evidence tables.
