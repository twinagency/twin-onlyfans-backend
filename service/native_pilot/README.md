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
(`DATABASE_URL`): a SQLite file as before, or PostgreSQL. The application schema stays at
revision 0091 (no migration): `native_pilots`, `native_pilot_inputs`,
`native_pilot_receipts`, `twin_bridge_writes` and the five `twin_bridge_*` control
tables live in their own PostgreSQL schema, `fastt_runtime`, defined by
`db/onlyfans_pilot_models.py` (a separate metadata, not in `check_schema`). The runner and
`native_pilot.control` create the schema and tables idempotently on start
(`store.ensure_runtime_schema`; additive, advisory-locked); the API/worker start-up and the
send boundary never do, and the runner refuses (`pilot-schema-missing`) if a table is still
absent. Rollback needs no schema change: the previous images' 0091 check ignores the extra
schema. Raw SQL reaches the tables through `search_path = fastt_runtime, public`. Every
write is one serialised transaction on both backends (`twin_bridge/store.py`). `tests/test_native_pilot_storage_pg.py`
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


## Persistent service (`native_pilot/service.py`)

The bounded pilot above is unchanged. `python -m native_pilot.service --account of-<owner> --fan <id>
--handle <name> [--twin-context] [--content-manifest FILE] --enable-send` runs the same gates as a
supervised process (compose service `fastt-runner`, profile `fastt`; without `--enable-send` it is a read-only
preflight). It runs until SIGTERM/SIGINT and is never started by itself.

- Cap: the account's existing `of_delivery_policy` (`limit_units`, `used_units`, `throttle_seconds`). Each
  admission spends one unit in the same transaction as the durable attempt; nothing resets or exceeds it. The
  conversation budget of the `svc-` pilot is a ceiling above any allowance, never the binding limit.
- Lease: renewed every 30 s (120 s TTL) instead of a deadline. A lapsed or lost lease stops sending; re-acquiring
  goes through `Ledger.activate_service`, which refuses a live owner, any unsettled send, or a conversation that is
  not `active`.
- Unknown outcome: sending stops for the fan, nothing is resent, the process keeps running and re-acquires only
  after an operator resolves the attempt (`twin_bridge_send_attempts`) and resumes the conversation.
- Clean stop releases the lease and keeps state and counts; a claim with no receipt is dropped so that message is
  answered after restart. A crash never regenerates its claim.
- Operator stop: `service.fan_hold_reason` (skip_list `manual_restrict` / `of_restricted` / `muted_creator`, or an
  `of_conversation_state` that is not `automated` / has a takeover) pauses answering; checked every tick, before
  admission and at the wire.
- Intake: `native_pilot/intake.py` runs FASTT's `OFWebSocket` on the same verified client and persists the scoped
  fan's plain text with `event_transcoder._transcode_chat_message` (idempotent upsert, keyed `of-<owner>`).
- Start: `FASTT_ACCOUNT=of-<owner> FASTT_FAN=<id> FASTT_HANDLE=<name> docker compose -f deploy/onlyfans-compose.yml
  --profile fastt up -d fastt-runner`.
