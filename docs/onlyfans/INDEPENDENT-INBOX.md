# Independent live inbox intake

Incoming messages must remain current while an account's AI is disabled and
while archive, ledger, media, or profile jobs are busy. The ordinary worker now
starts a dedicated inbox scheduler with a separate database pool. It discovers
verified bindings every five seconds and runs at most one read task per account,
with eight accounts able to progress independently.

The live head is checked approximately every ten seconds when the provider is
responsive. Message bodies are fetched before directory pagination. A newer head
for an already queued fan is promoted; a failed conversation is deferred for a
minute without blocking other fans. A newer message cancels that local retry
delay. Directory work remains bounded and cannot consume an ever-growing queue.

These are read and import changes. Activation, human takeover, no-contact state,
delivery limits, purchase/ownership checks and sender selection remain with their
existing owners. Disabled accounts use historical import semantics and create no
reply work. Active accounts retain their existing canonical catch-up semantics.
Provider leases, shared rate-limit backoff, connection-generation fencing and
message deduplication remain mandatory.

`ONLYFANS_RECENT_INBOX_ENABLED` defaults to enabled for the independent scheduler.
An explicit `0` disables it. The main background worker skips its duplicate head
poll while the independent scheduler owns that work. `--once` retains the bounded
existing synchronous diagnostic behavior.

The worker writes `/tmp/onlyfans-inbox-health.json` atomically. This contains each
account's last successful provider head read, queue length and sanitized error
type. It contains no session values, message bodies or fan identifiers. A head
read older than 120 seconds is stale. `inbox_worker.healthy()` also rejects a dead
scheduler or a roster refresh older than a minute. Configure the worker image's
health check to call `inbox_health.healthy()` with `/app/service` on `PYTHONPATH`.
The health module uses only the standard library, avoiding a second application
and ORM import on every probe. The worker has a 768 MB memory allowance and one
CPU; its independent inbox database pool has eight slots to match its readers.

The scheduler retries failed reads automatically. Provider outages, expired
sessions, explicit rate limits and sustained load beyond configured capacity can
still delay incoming messages; the health signal exposes those conditions rather
than equating a running process with a current inbox.

Validation uses disposable PostgreSQL and synthetic provider responses:

```sh
PYTHONPATH=service:service/tests python -m pytest -q \
  service/tests/test_onlyfans_recent_inbox_pg.py \
  service/tests/test_onlyfans_independent_inbox_pg.py \
  service/tests/test_onlyfans_readonly_inbox_pg.py \
  service/tests/test_onlyfans_launch_provider_budget_pg.py
```

For a focused worker deployment, preserve the running image as rollback and
replace only the worker service. Check the real deployed source against the
chosen parent commit before applying the patch. Do not run migrations or replace
the API, dashboard, sender supervisor or saved connection state for this change.
