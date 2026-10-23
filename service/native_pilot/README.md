# Native Fastt text experiment

This is a manually started, single-chat experiment, not the production consolidation.
The actual `automations.ai_chatter.run` builds prompts, gathers local history, generates
and shapes replies, applies its eligibility rules and records native attribution.
Only this dedicated process receives temporary configuration and transport adapters.
No account-wide enablement or provider key is saved.

The approved experiment uses account `176774528` (`@niickybabyfree`), fan `318022380`
(`@u318022380`), and at most **ten new outgoing messages**, including separate bubbles.
The session, fixed proxy and model credential are read (read-only, fail closed) from the
account's verified `of_provider_connection` record; no house/dotenv key and no session
file is used (see "Runtime mapping" below). The model and provider order match Twin;
prompts, temperature and response format are Fastt's. No Twin draft/status/ack endpoint or Twin persona is involved.

## Storage

The ledger, ownership reservation and writer markers run on the application database
(`DATABASE_URL`): a SQLite file as before, or PostgreSQL at schema 0092
(`0092_native_pilot_storage` creates `native_pilots`, `native_pilot_inputs`,
`native_pilot_receipts`, `twin_bridge_writes` and the five `twin_bridge_*` control
tables). Every write is one serialised transaction on both backends
(`twin_bridge/store.py`). PostgreSQL never runs runtime DDL; the runner refuses
(`pilot-schema-missing`) if a table is absent. `tests/test_native_pilot_storage_pg.py`
runs the whole ledger and runner suites unchanged against a real PostgreSQL.

## Boundaries

- Fresh input only, after activation; seven seconds of quiet before generation.
- Exact account and fan; no `force_ids`, unrestricted client or account-wide scheduler.
- Text only. Media, offers, stickers, scheduled follow-ups, nickname changes, read
  indicators and typing WebSocket calls are disabled or refused in this process.
- The native default voice applies when no creator profile is configured. Invented
  day-log generation is disabled. This is not a completed creator-personality test.
- Durable admission before each single POST. Unknown delivery stops the experiment
  without resending. Confirmed raw receipts survive native bookkeeping failure.
- Original bridge attempts and spent allowance remain. Activation adds the approved
  allowance once and installs exclusive ownership atomically. Normal completion or
  stop revokes unused allowance and leaves the conversation paused/reserved.
- Maximum 30 minutes and 80 model calls; each call has a 45-second deadline, 400 output
  tokens and no automatic retry. Truncated, invalid or failed model output stops work.
- Native blacklist, manual takeover, lease and other eligibility checks remain. A
  refused turn ends the experiment rather than overriding those checks.

Keep the Twin worker off. Use the test chat exclusively: the local reservation
cannot fence another device or provider-scheduled activity. The sender checks live
history and local source/ownership immediately before posting, but cannot promise an
atomic transaction with the remote platform.

## Commands (from `service`, supported Python 3.13 environment)

Preflight, without activation or model calls:

```powershell
& "$env:TEMP\of-preflight\venv\Scripts\python.exe" -B -m native_pilot.runner --account of-176774528 --fan 318022380 --handle niickybabyfree --pilot-id YOUR_PILOT_ID --send-limit 10
```

Live start uses those exact arguments plus `--enable-send`. Use a new, recorded pilot
ID for each explicitly authorized experiment. Do not automatically restart a process
or invent a new ID to bypass an exhausted allowance, paused state or unresolved send.

```powershell
& "$env:TEMP\of-preflight\venv\Scripts\python.exe" -B -m native_pilot.control status --pilot-id YOUR_PILOT_ID
& "$env:TEMP\of-preflight\venv\Scripts\python.exe" -B -m native_pilot.control stop --pilot-id YOUR_PILOT_ID
```

`stop` prevents further admission. A POST already in flight may still finish; its
receipt must be reconciled, never retried. If the process is killed abruptly, use
status/stop and inspect unresolved evidence before any worker is restarted. The
legacy Twin worker does not understand native pilot records; do not start it while
this experiment or its unused grant is unresolved.

## Runtime mapping (Twin live database)

- **Account ids.** Live tables key the account `of-176774528`; OnlyFans and Fastt's
  provider side use `176774528`. `scope.AccountScope` is the single boundary: the DB id
  goes to the ledger, `ai_chatter`, budget and catalogue; the provider id goes to the
  client, history and receipts. Live data is never renamed. A bare decimal maps to itself.
- **Session.** `connection.load_verified_connection` requires status `verified`, no
  check error, unexpired lease, matching owner/binding, and decrypts with
  `ONLYFANS_CONNECTION_KEY` from the process environment only. `build_client` makes the
  `OFClient` with the saved proxy and signing hash (no warm-up request). The legacy
  role guard is opened for that one owner id, in a `worker`-role (or role-less)
  process only (`role_guard.allow_native_pilot_provider_access`).
- **Model and budget.** The key is the account-scoped credential sealed with the
  connection; its model must equal the saved `ai_chatter` model and the pilot's pinned
  wire model. Each call reserves in the existing `grok_daily_cost` row against
  `daily_cost_cap_cents` before any HTTP, settles on provider usage, and keeps the
  reservation (call marked `unknown`) when the outcome is unproven.
- **Attribution.** AI sends carry `automation_kind` and the `Automation` sentinel; human
  sends projected by Twin carry the `Twin staff` sentinel. Both rows are created only by
  `python -m native_pilot.attribution seed` (explicit); send mode refuses until they exist.
- **Content.** `content.ContentPlan` accepts exactly one reviewed fixed $3 card (all its
  media, its previews, `locked_text` false) from a private manifest and refuses any other
  shape, price, item or receipt.

## Verification

The `tests/test_native_pilot_*.py` scripts run with temporary synthetic databases,
stubbed models/transports and no live credentials. They cover the native engine,
provider adapter, persistent cap/ownership/source gates and assembled sender. They do
not establish production scale, native paid-media sales or guaranteed conversion.
