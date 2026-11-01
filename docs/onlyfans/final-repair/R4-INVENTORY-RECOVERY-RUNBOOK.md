# R4 inventory import / recovery runbook (prepared, NOT executed)

Applies to any creator whose account shows zero (or implausibly few) imported vault items while media permission is on
("creators with zero imported items"). No account ids appear here. Nothing below runs during the implementation phase.
It is executed only in the approved release phase, by the designated operator, with read-only checks first.

## Rules
- No cursor resets, no second import for a job that is still `pending`/`running`, no provider-limit bypass, no manual
  token or backoff edits, no AI enablement change. Importing history or inventory never enables an account.
- Prefer the existing paths: operator "sync vault" command (`request_sync`) -> `process_vault_jobs`, then the describe
  worker. Use bounded read-only SQL only for diagnosis.

## 1. Diagnose (read-only)
1. Connection: `of_provider_connection` row `status='verified'`, `expires_at` in the future, `last_error IS NULL`.
   If not verified, stop: re-verification is the fix, not an import.
2. Items: `SELECT count(*), count(*) FILTER (WHERE removed_at IS NULL) FROM vault_items WHERE account_id=:a`.
3. Job state: latest `of_vault_sync_job` for the account: `status`, `pages_read`, `items_seen`, `page_offset`,
   `last_error`, `lease_until` (retry deadline when deferred), `requested_at`, `updated_at`.
   - No row ever: nobody requested a sync. Go to step 2 (request it).
   - `failed` with `vault_job_expired`: it was starved/idle for over an hour. Cause is capacity (step 3), then re-request.
   - `failed` with `connection_unavailable` / `vault_read_failed` / `unsupported_vault_*`: fix the cause first.
   - `running`/`pending` with `provider_read_deferred:<cause>[:<lane>]`: the import is alive but waiting. Causes:
     `lease-occupied:<lane>` (another lane holds the account lease), `provider-backoff` (shared provider backoff; wait),
     `budget-exhausted` (intake or another lane is using the tokens), `ledger-turn-due`, `starved-lane-turn`.
4. Budget row: `of_provider_read_budget` for the account: `tokens`, `backoff_until`, `last_kind`, `lease_until`,
   `starved_kind`, `starved_since`. With the R4 fairness change a starved vault lane is owed a turn every ~20-65 s.
5. Description readiness: count items with a meaningful description (not `search_text` = bare `photo`/`video`/`audio`),
   `describe_status` distribution, and `vault_ai_config_json.auto_describe_on_import`.

## 2. Request the import (only if no active job and the connection is verified)
Use the operator vault "sync" action (idempotent `command_key`). Do not insert job rows by hand. One active job per
account is enforced by the unique active index; a duplicate request returns the existing job.

## 3. If the job defers for a long time
- Watch `last_error`/`lease_until`/`pages_read` for 10 minutes. `pages_read` must increase at least every ~2 minutes
  under normal load (deployment with R4 fairness). If `provider-backoff`, wait; do not clear it.
- If there is a very large unread inbox backlog for the account, the recent-inbox lane is the heavy user; the fairness
  turn keeps vault moving at a trickle. Do not stop the inbox worker.
- Do not edit `of_vault_sync_job.page_offset`/`items_seen` (they have a check constraint; a reset would re-read
  already imported pages and is a cursor reset).

## 4. Describe
After items exist, descriptions come from the existing describe worker (`auto_describe_on_import` + env include lists).
Items stay "not ready" (never offered, never claimed) until they have a meaningful description, a known version
(`updated_at_of` or `content_hash`) and a price source. Check readiness with `product_readiness.item_readiness`
over a sample; the `missing` tuple names the reason.

## 5. Verify before the account enters any paid pilot
- Item count within 2% of what the creator sees in the provider vault (operator confirms by sight; a number from the
  job alone is not proof).
- >= 90% of items `ready` per `product_readiness` (or the shortfall explained: removed, boundary-excluded, no price).
- A sample of 20 items checked by a human against the actual media for description, format and count.
- Purchase coverage current (ledger scan complete) and creator boundaries/limits loaded.
- Cristina and Amelia (or any such creator) do not enter the paid pilot until all of the above hold.

## Stop conditions
Wrong-account data in `vault_items`; repeated `unsupported_vault_pagination`; provider 429 storms (honour backoff);
any request to bypass limits. Record the evidence and escalate; do not retry blindly.
