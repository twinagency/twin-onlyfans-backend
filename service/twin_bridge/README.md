# Twin bridge (Stage 1C)

Offline Fastt -> Twin pieces:

- **durable intake** (`intake.py`, `normalize.py`, `scope.py`, `reconcile.py`) -
  records eligible inbound fan DMs that Fastt has already persisted, as pending
  bridge work;
- **HTTP client** (`client.py`) - an import-safe async client for Twin's draft
  and status routes, called explicitly by tests (or, later, by a worker).

- **durable turns** (`turns.py`) - the 7-second quiet window, frozen membership
  and the exact event bytes Twin receives;
- **worker** (`worker.py`) - the state machine from frozen turn to confirmed
  delivery and acknowledgement; one bounded pass per call;
- **coordination** (`coordination.py`) - durable per-conversation ownership, the
  turn claim fence, and the shared-transport boundary that denies competing
  writers;
- **delivery** (`delivery.py`) - the send-attempt ledger, the pilot send budget
  and the last-instant dispatch gate;
- **platform** (`platform.py`) - the account-bound OnlyFans plain-text adapter
  (exactly one message POST per attempt);
- **manual controls** (`cli.py`) - status / pause / resume / takeover / run-once
  / recover;
- **automatic pilot** (`pilot.py`) - serial worker passes in one process, for one
  explicitly selected account/fan, with a durable send cap and runtime bound.

Intake never calls Twin. Nothing here contacts OnlyFans unless an operator
explicitly runs `run-once --enable-send` or the automatic pilot with send permission
and a positive pilot send limit configured. `twin_bridge_inputs` stores SOURCE inputs; the new tables store what
was decided about them.

## Automatic text pilot

With the relay and Twin already running, start once from the `service` directory:

```text
python -m twin_bridge.pilot --account ACCOUNT_ID --fan FAN_ID --enable-send --send-limit 3
```

The account must match configuration and the fan must be the entire configured
allowlist. The requested limit cannot exceed the configured limit or increase
the existing durable budget. An existing zero cap stays disabled. The default
runtime is 900 seconds; `--max-seconds` may select up to 3600. The deadline stops
new passes and lets an admitted pass settle. Imports start nothing.

One process advances generation, sending and acknowledgement every two seconds,
so subsequent incoming messages need no per-reply command. A current ready draft
can send on the first pass. Parked/cancelled drafts are never reactivated. The
existing seven-second incoming-message quiet window remains. Console output
contains progress counts and fixed stop reasons, not message text or credentials.

The pilot stops at the total durable cap, on pause/attention, a newly parked turn,
unknown/in-flight delivery, unrelated open conversation work, a configuration
change, an error, or the time bound. A confirmed send awaiting acknowledgement
can finish its bounded ack-only recovery even at the cap or during a pause;
it cannot repeat the provider POST. Ctrl+C stops the worker; a send already in
flight cannot be recalled and retains its delivery evidence. Restart never
resets the budget or automatically resumes a paused conversation.

## Configuration

All values come from the process environment. Nothing in a message can set them.

| Variable | Meaning |
| --- | --- |
| `TWIN_BRIDGE_ENABLED` | `1`/`true`/`yes`/`on` to enable. Anything else, or unset, is **disabled**. |
| `TWIN_BRIDGE_ACCOUNT_ID` | The ONE Fastt account this bridge serves. No active-account fallback. |
| `TWIN_BRIDGE_CREATOR_SLUG` | The Twin creator slug that account answers as. Never taken from a payload. |
| `TWIN_BRIDGE_FAN_ALLOWLIST` | Comma-separated fan ids. Empty means nobody is designated. |
| `TWIN_BRIDGE_ACTIVATION_CUTOFF` | Required, timezone-qualified ISO-8601 (e.g. `2026-10-15T00:00:00Z`). |

Incomplete or malformed configuration resolves to **disabled** (with the reasons
logged once), so a bridge mistake can never break ordinary ingestion. The cutoff
is never defaulted to "now": a silent reset on restart would turn a replayed
backlog into new work.

The client takes its base URL and bearer token from trusted configuration /
dependency injection:

```python
client = TwinBridgeClient("http://127.0.0.1:4311", token)
```

Only `http://127.0.0.1:<port>` or `http://[::1]:<port>` is accepted. `localhost`,
remote hosts, embedded credentials, query/fragment-bearing values, a path, and an
invalid or missing port are refused. Tokens must be at least 32 characters with
no whitespace. The client disables redirects and ambient proxy configuration so
the token cannot be forwarded elsewhere.

## Response validation

A reply is only handed to the caller as a draft when it is a complete, correctly
typed version-1 projection that satisfies Twin's own invariants:

- `schemaVersion` must be the INTEGER `1`. A boolean is refused even though
  `True == 1` in Python, in both the success and the error envelope.
- Every key Twin's `projectReply` always emits must be present - `requestId`,
  `conversationKey`, `status`, `outcome`, `deliverable`, `text`, `epoch`,
  `createdAt`, `completedAt`, `deliveredAt`, `committedAt` and `usage` - and so
  must all six usage keys. An explicit `null` stays legal wherever the lifecycle
  permits it; an ABSENT key never does. A missing `deliveredAt`/`committedAt` is
  not evidence of no delivery, so it cannot become a deliverable draft.
- Types are checked before membership or hashing, so a list, object or null in a
  field produces one fixed sanitised failure rather than a raw Python exception.
  `epoch` and the usage counts are real integers (never booleans) and the usage
  figures are finite and non-negative; a JSON integer too large for a float is a
  conversion failure, not a value to clamp.
- `deliverable` is exactly Twin's own `live` predicate: completed, undelivered,
  uncommitted, with non-empty text. A `failed` outcome, a non-null delivery or
  commit stamp, or malformed accounting can never be returned as a draft, while
  pending, cancelled, interrupted, blocked, delivered and
  decision-without-commit projections all remain valid.
- Timestamps must be canonical, timezone-qualified instants. A year-zero value, or
  an offset that would normalise outside the supported range, is the same fixed
  failure rather than a leaked `ValueError`/`OverflowError`.
- `usage.provider` is a LABEL, not an identifier: Twin accepts any nonblank string
  (`src/channels/service.ts`) and projects it verbatim, so `synthetic/provider` is
  legitimate and is preserved byte-for-byte. Blank or non-string metadata is
  refused. Identifier rules (no slashes, no padding, no control characters) are
  deliberately not applied here.

Failure category and admission uncertainty are separate. Only a proven pre-send
failure (`ConnectError`, `ConnectTimeout`, `PoolTimeout`) means Twin did not
admit the request. A write error, a timeout, a protocol cut, or a failure to close
a response whose body was already read leaves uncertainty set, because the work
may already have been admitted; a malformed or oversized reply is likewise
uncertain and never becomes a refusal. Non-200 responses are decoded as a
versioned error envelope and must carry a known code with the one status that code
is documented with - an arbitrary non-JSON 5xx, an unknown code or an `ok: true`
error body is an unverified response, not a known state. Every call makes exactly
one attempt: no retry, no polling, no fallback, and no delivery inference.

## Eligibility and the cutoff

A message becomes pending bridge work only when **all** of these hold:

1. Scope is enabled and the `(account_id, fan_id)` pair is the configured
   account plus an allowlisted fan.
2. The provider frame is readable at all. `message.id` and `fromUser.id` must be
   real JSON integers (never a bool, float or string), and both must still equal
   the persisted canonical ids. Nothing is `int()`-coerced on the way in: that
   is what let `1002.9` become `1002` and `True` become `1`.
3. `direction == 'in'` and `responseType` is absent or `message`. Self echoes,
   outbound rows, reactions, system rows and other shapes are out.
4. Not a tip, not paid, and not locked (`lockedText` is false, `price_cents == 0`,
   `is_paid` is not `True`).
5. No media at all: `media_count == 0`, no media ids, and not a Giphy GIF - even
   when the message carries a caption.
6. Not unsent/deleted.
7. The provider timestamp is present, valid, and **at or after** the activation
   cutoff.
8. Fastt's first receipt (`ingested_at`) exists **and is also at or after** the
   cutoff - a scan or a duplicate is not a new receipt.
9. The HTML-normalised text is non-empty and at most 4000 UTF-16 code units.
   Oversized text is refused outright, never truncated into an apparently
   complete event.

The same `eligibility_problem` predicate answers this for BOTH callers. The hook
hands over the live provider frame; the reconciliation re-parses the frame
Fastt stored in `messages.raw_json`. The two therefore cannot disagree - a GIF
the hook refuses can never be admitted by a later scan. A frame that is missing,
unreadable or ambiguous is refused, never upgraded into eligible text.

Timestamps: `occurredAt` comes from the **original provider `createdAt`** string,
so a non-UTC offset is converted rather than stripped (the transcoder's stored
column has already lost the offset). There is no fallback to the transcoder's
synthesised `now`: a DM with no readable provider instant is refused rather than
treated as fresh work. `receivedAt` is always Fastt's first `ingested_at`.

Identity: only `TWIN_BRIDGE_CREATOR_SLUG` is served, and it is copied onto every
row so a later configuration change cannot reassign existing work. For this
account/fan chat shape `conversationId = fanId`; account and creator stay
separate identity components.

Trusted scope is validated against what its own SQL can execute:
`TWIN_BRIDGE_FAN_ALLOWLIST` entries that are not plain decimal BigIntegers
disable the scope (with the reason logged) instead of failing later at an
`int()` conversion.

## Storage and deduplication

`twin_bridge_inputs` is additive, keyed on
`(account_id, fan_id, provider_message_id)` - the same tuple as the canonical
`messages` row. That key is the database unique constraint the ingestion hook
and the reconciliation both rely on, so a duplicate or a racing admission cannot
create a second row.

`event_json` is an immutable versioned snapshot. A later edit of the canonical
row never rewrites it. A duplicate with an **identical** snapshot is reported as
a duplicate; a **contradictory** same-id payload raises `IntakeConflictError` -
the row is never overwritten, reset or reinterpreted as new work.

The hook writes inside the transcoder's own transaction, after the
message/fan/chat writes. It never commits separately, never launches a task and
never makes an HTTP call. A database failure propagates, the transaction rolls
back, and neither the new message nor a partial intake survives. The bridge token
is never persisted.

## Recovery limits

`reconcile_pending(scope=..., batch_size=..., after=...)` is explicitly invoked
and bounded (default 200, hard cap 500). It:

- scans only the enabled scope, designated fans and messages whose Fastt receipt
  is at/after the cutoff. The SQL prefilter uses `ingested_at`, Fastt's OWN
  column, which has no provider offset to lose; the provider boundary is applied
  in Python from the verified `createdAt`, so a correctly converted provider time
  can never be discarded by SQL;
- orders by the total order `(ingested_at, fan_id, message_id)` - `fan_id` alone
  is not unique, so a partial sort key would not be a stable page boundary - and
  pages by keyset from the same three components;
- compares both times as TEXT, so a page keeps advancing past an unreadable
  timestamp instead of aborting, and never re-parses a row already identified as
  dirty through the ORM's DateTime processor;
- skips rows that already carry an intake entry, so it makes progress past them
  and a repeated run admits nothing new;
- goes through the same admission path and unique constraint as the hook, so it
  is safe to repeat or race with it.

It starts from **committed Fastt messages only**. Fastt's upstream event queue can
drop an event before canonical persistence; nothing here can recover a message
Fastt never stored. There is no automatic historical sweep and nothing runs on a
timer.

## Turns, ownership and delivery

### Configuration (synthetic examples only)

| Variable | Meaning |
| --- | --- |
| `TWIN_BRIDGE_SEND_ENABLED` | `1`/`true`/`yes`/`on` to permit the platform POST. Separate from `TWIN_BRIDGE_ENABLED`. |
| `TWIN_BRIDGE_SEND_LIMIT` | The pilot cap. It must be a positive integer or NOTHING may send. |
| `TWIN_BRIDGE_BASE_URL` | Twin's loopback adapter, e.g. `http://127.0.0.1:4311`. |
| `TWIN_BRIDGE_TOKEN` | The bearer token (>= 32 chars, no whitespace). |
| `DATABASE_URL` | Explicit absolute SQLite URL shared by the relay and every operator CLI process. Keep it in the ignored `service/.env`; do not rely on different process defaults. |

`run-once` forms turns and asks Twin for drafts even with sends disabled; it only
dispatches when BOTH the flag and a positive limit are set. Leaving the token,
base URL or limit unset permits no send.

**Configuration precedence (one direction only, no default allowance).** With
`run-once --enable-send`, a POSITIVE limit is mandatory: `--send-limit N` on the
command line wins, otherwise `TWIN_BRIDGE_SEND_LIMIT` from the environment. There
is no fallback of `1`. An **explicit** CLI cap is authoritative: `--send-limit 0`
or a negative value refuses the run (`invalid-send-limit`) BEFORE any form/submit/
dispatch work, even when the environment has a positive cap. Only an ABSENT CLI
cap falls back to `TWIN_BRIDGE_SEND_LIMIT`; if that is also absent/zero the run
refuses (`send-limit-required`). The effective cap is stored durably per
conversation as the MINIMUM of what is already persisted and the configured value
(`coordination.set_budget_limit`), so a restart or a different invocation can
only tighten the pilot cap - it can never silently raise it.

### Durable states

A **turn** moves through: `frozen -> submitted -> draft_ready -> dispatching ->
sent -> delivered`, plus `delivery_unknown`, `cancelled`, `parked` and
`attention`. A **conversation** is `active`, `paused`, `attention` or
`disabled`. Two fences are deliberately separate:

- `reserved` is the DURABLE conversation reservation. It stays held while the
  bridge owns the conversation - including while paused / attention /
  `delivery_unknown` and across a restart - so competing local writers, schedule
  and mass sends stay refused. It is cleared only by an EXPLICIT release: the
  conversation resolving with no unresolved turn, or a recorded human takeover.
- `revision` is the INVALIDATION fence. Takeover, pause, cancel, reset and a
  scope change advance it, so an already-accepted draft becomes obsolete. A
  legitimate worker REPLACEMENT (a new process taking an expired claim) does NOT
  advance it, so an unchanged, already-accepted draft survives a restart instead
  of being parked.
- `_ACQUIRING` is the ADMISSION token, held only inside the owning process. A
  reservation takes it atomically with the "no local writer in flight" check, and
  every competing transport write is refused while it is held. That is what
  closes the in-process quiescence->reservation race. Across PROCESSES the
  durable `BEGIN IMMEDIATE` arbitration below is what serialises admission; the
  `_ACQUIRING` token alone never had cross-process authority.

### History boundary for a frozen turn

The external-writer check covers the interval beginning at the earliest provider
timestamp of ALL frozen source inputs. Receipt order can differ from provider
order. A creator reply strictly before that boundary is historical context;
an equal/newer creator reply still parks the draft unless its provider ID is
proven sent by the bridge's own ledger. Identity and direction are validated
before the time exemption. Unreadable timestamps refuse the send, and times
within the same millisecond are treated conservatively as equal.

The real adapter walks descending history using the provider's older-ID cursor.
It must observe every frozen source ID and either cross strictly behind the
boundary or reach an explicit `hasMore=false`. A short page alone is not proof
of completion. Missing source rows, invalid timestamps, inconsistent ordering,
duplicate/non-progressing cursors, or exhaustion of the 20-page/1,000-item bound
refuse delivery. This remains a snapshot check, not an atomic fence against a
remote message arriving after the read.

Previously parked turns are not reopened by this fix. A fresh fan input creates
a new turn; the prior refusal and its unspent budget remain recorded.

### Process topology and cross-process admission

The supported local arrangement is the Fastt process OR the documented
`python -m twin_bridge.cli run-once` runner - both are local writers against the
same SQLite database. They are NOT left to a process-local fence. Admission is a
SHARED ATOMIC protocol:

- A local DM write through `OFClient.send_message` (and the scheduled / mass
  senders) performs its "is the conversation reserved?" check AND its marker
  insert in ONE `BEGIN IMMEDIATE` SQLite write transaction, then POSTs.
- Ownership admission (`reserve_ownership`) performs its "is a writer in flight?"
  check AND its reservation `UPDATE` in ONE `BEGIN IMMEDIATE` transaction.
- SQLite serialises write transactions, so the two cannot interleave: whichever
  commits first wins and the other side sees it. The admission COMMIT is the
  ordering boundary; neither transaction is held across a network call.
- A broadcast registers ACCOUNT-WIDE (fan `0`) and is refused while ANY
  conversation for the account is reserved; a targeted admission sees a live
  broadcast marker for the account in both orderings.
- **Marker lifetime (uncertainty is retained).** A `held` marker is RENEWED on a
  heartbeat for as long as the POST is genuinely in flight. It is DELETED only
  for a PROVEN settled outcome: a clean return, a proven pre-send failure
  (`requests` `ConnectionError`/`ConnectTimeout`, i.e. never connected), or a
  provider 4xx other than 408/429 (answered and refused). Any other exception,
  and a crash mid-POST, leaves a marker that admission must keep honouring: an
  exception turns the marker into `unresolved`, and a `held` marker whose holder
  stopped renewing is PROMOTED to `unresolved` rather than allowed to lapse. A
  TTL never turns an in-flight/uncertain write into "the chat is clear".
  `unresolved` markers can only be cleared by an explicit reconciliation
  (`twin_bridge.cli reconcile-writes --fan N`).
- **Admission itself holds a real write transaction.** `begin_attempt` opens an
  explicit `BEGIN IMMEDIATE` on its own connection BEFORE reading the canonical
  sources, and holds it through the conditional budget reserve and the attempt
  insert. A competing source invalidation on any other connection therefore
  cannot commit in the validation->admission window: invalidation first means no
  attempt / no budget / no POST; admission first keeps the existing
  in-flight/unknown semantics. No network call runs inside the transaction.
- The bridge's own dispatch uses `send_text_once` (which does not publish a
  marker) and, for the owned dispatch path, treats its uncertainty as already
  carried by the attempt ledger and the paused conversation, so it never leaves a
  second, permanent marker that would deadlock its own recovery.

FAIL-CLOSED: if coordination storage cannot be read or written, that is NOT
proof the chat is clear. An ENABLED bridge refuses both the reservation and a
competing local write rather than silently falling back to process-local
protection; a DISABLED / out-of-scope bridge keeps ordinary Fastt behaviour.

A genuinely EXTERNAL writer (the official app, a provider-scheduled message, a
different machine) is still outside any local fence and stays an explicit
live-pilot exclusion.

The expiring `owner_token` / `owner_expires_at` claim is separate again: it is
the per-attempt worker claim, and pausing/attention clears the claim while
keeping the reservation.

| Durable state / event | What the worker does next |
| --- | --- |
| Frozen turn, no submission | Claim the turn, submit the frozen event once. |
| Submission timeout / unusable reply | Keep the same event and id; read status. Never regenerate. |
| Twin `queued`/`in_progress` | Schedule bounded status reads (>= 2s apart, max 60). |
| Twin `not-found` after uncertain admission | One bounded re-submission of the SAME event id; Twin dedupes. |
| Completed, deliverable `ok`/`blocked` draft | Persist text, ids and epoch; record the conversation revision the draft was accepted under. |
| `failed` / `interrupted` / `unsupported` / `stale` / no draft | Park + pause. No fallback text, no fresh id, no send. |
| Twin `cancelled` | Keep the turn cancelled; the conversation keeps its takeover state. |
| Draft ready, sends disabled | Wait (no attempt row, no POST). |
| Cancelled / resume boundary | UNSENT turns move to a durable `cancelling` reconciliation (bounded `cancel_attempts`, Twin-cancelled using the frozen event even when no request id was returned); `sent`/`dispatching`/unknown work is never touched. |
| `cancelling` | Bounded reconciliation (2s then 5s, max 3): a status read plus `cancel_request` against the FROZEN identity each pass. A `not-found` does NOT prove non-admission (a cancel can arrive before a still-in-flight submit is admitted), so reconciliation continues within the bound and cancels a late admission; exhaustion parks for attention. Never dispatchable, so a late admission cannot resurrect it. |
| Cancelled WITH a newer control boundary | The boundary is the NEWEST of `reset_at`/`cancelled_at`, so a cancel after a pause/resume still invalidates a draft frozen in between. |
| Dispatch authorized | Reserve the budget and write `dispatching` BEFORE the POST. |
| Conditional transition refused | Hold and refuse: an authorization is NEVER minted after a failed conditional transition. |
| Fan on the existing cooldown / skip-list | Hold before the lease: the bridge respects `fans.automation_paused_until` and `skip_list` exactly as every other sender. |
| Shared lease held by another writer | Hold (never POST) until it is free. |
| Verified created-message id | Persist `sent` + the exact provider id, then acknowledge. |
| Missing/invalid id, wrong recipient, timeout, ambiguous failure | `delivery_unknown`, pause the conversation, no ack, no automatic resend. |
| Crash while `dispatching` (older than the attempt bound) | Recovered as `delivery_unknown`; lease expiry never authorizes another send. A row younger than the bound may be a LIVE dispatch and is left untouched. |
| Crash AFTER `mark_sent` but before the turn update | Reconstructed to `sent` from the confirmed attempt evidence, then ack-only: no second POST. |
| Resume / operator cancel | Unsent pre-boundary turns enter `cancelling`; PENDING inputs RECEIVED before the boundary are parked. A resumed conversation admits only inputs received after it. |
| Sent, ack missing/failed | Retry only the idempotent ack/status (3 attempts, 2s then 5s). Never re-POST. |
| Ack `delivered`/`already-delivered` + commit confirmed | `delivered`. |
| Attribution/cache failure after a confirmed send | Ledger stays `sent`; bookkeeping is retried independently. |

### Retry bounds

At most **3 draft submissions** for the same frozen event (2s then 5s), **60
status polls** (>= 2s apart), **3 ack attempts** (2s then 5s) and **3 cancel
attempts** (2s then 5s). Counters and `next_action_at` are persisted, so a
restart or a repeated `run-once` cannot reset a budget. Exhaustion parks the turn
for attention. The platform POST is never retried automatically.

The counters that bound an EXTERNAL operation are written BEFORE that operation:
`generation_attempts` is committed before the draft request, `status_polls`
before the status read, and `ack_attempts` before the acknowledgement. An
interruption therefore still counts against the durable bound instead of
resetting to zero on restart.

### Manual commands

```bat
rem status for the configured account (or --account / --fan to narrow)
python -m twin_bridge.cli status --fan 9001
python -m twin_bridge.cli pause --fan 9001 --reason operator-pause
python -m twin_bridge.cli resume --fan 9001
python -m twin_bridge.cli takeover --fan 9001        rem a human took the chat
python -m twin_bridge.cli cancel --fan 9001          rem tombstone unsent work
python -m twin_bridge.cli reconcile-writes --fan 9001  rem clear an unresolved local-writer marker
python -m twin_bridge.cli recover                    rem dispatching -> unknown
python -m twin_bridge.cli run-once                   rem form + draft only
python -m twin_bridge.cli run-once --enable-send --send-limit 1   rem explicit cap
```

`python -m twin_bridge.cli run-once` runs in its OWN process, distinct from the
Fastt service. That is a supported local arrangement: the cross-process
admission above makes a Fastt-process writer and the CLI runner arbitrate
against each other, so the older writer must settle (or the reservation is
refused). Do NOT run it on a different machine from the Fastt database - a
remote writer is outside the durable fence.

`--fan` may be given either before or after the subcommand
(`cli --fan 9001 pause` == `cli pause --fan 9001`). `takeover` records a human
takeover and exits non-zero (blocked) when a bridge attempt is in flight or its
outcome is unknown, so a manual reply can never race a live dispatch.

### Writer coverage

Ownership is enforced where a DM is actually POSTed, so a competing writer that
already passed its own eligibility check is still stopped.

| Writer path | Covered? | How |
| --- | --- | --- |
| Direct DM (`OFClient.send_message`) | Yes | Shared transport guard: refuses while the conversation is reserved (process-local OR durable, so it survives a restart). |
| Scheduled single send (`OFClient.schedule_message`) | Yes | Same guard (`purpose="scheduled"`), refused while reserved. |
| Mass / scheduled mass (`send_mass_message`, `schedule_mass_message`) | Refused for a reserved account | Their audience cannot be shown to exclude the reserved fan, so a `broadcast`-scope write is refused account-wide. |
| Manual Fastt composer (`server.py` send route) | Yes | The route calls `human_takeover` BEFORE dispatch: a normal draft is taken over (reservation released, unsent work fenced); an in-flight/unknown attempt makes the takeover LOSE its arbitration and the route refuses with 409. The route's decision->guarded-transport seam is tested; `server.py` itself cannot be imported under the Python 3.11 harness (see limits). |
| Bridge's own dispatch | Only with a live authorization | `guarded_send(purpose="twin_bridge")` requires the attempt-bound token from `authorize_dispatch` (account/fan/turn/attempt/claim/revision), minted only after the `draft_ready -> dispatching` conditional transition SUCCEEDS. There is no `purpose`-string bypass. |
| Shared per-fan lease | Yes | The bridge acquires the SAME `fan_lease` row the automations use, with a unique holder token, and refuses to POST when another live holder owns the fan. |
| Existing cooldown / skip-list | Yes | `automation_executor.fan_on_cooldown` (`fans.automation_paused_until`) and the `skip_list` "don't auto-reply" row are checked before the lease. |
| Human/local outbound in history | Yes | During the frozen input interval, only the bridge's OWN proven sent ids are exempt. Locally attributed human replies still park the draft; strictly older, validated replies are historical context. |
| A LOCAL sibling process (`python -m twin_bridge.cli`, any other local `OFClient`) | Yes | Shared atomic admission: the writer's reservation-check + marker insert and the bridge's writer-check + reservation UPDATE are each a single `BEGIN IMMEDIATE` SQLite transaction, so they serialise across processes (both orderings tested with real sibling processes). |
| Account-wide broadcast from a local writer | Yes | Registered account-wide (fan `0`); conflicts with every targeted fan in the account in both orderings. |
| Provider-scheduled messages, other MACHINES, the official app | **No** | Genuinely remote from any local fence. Explicit live-preflight exclusion. |
| History authored by neither the bound creator nor the bound fan | Refused | `external_outbound_ids` requires the supplied author to be the bound creator or the bound fan; anything else is `history-unreadable`, not assumed inbound. |

### Limits and live prerequisites

- The default is disabled: no worker runs, nothing is consumed, nothing is sent.
- The first pilot has **no automatic retry after the message POST boundary**.
- The bridge now validates any supplied created-message `fromUser`/`withUser`
  evidence against the bound creator/fan. If the real `send` response ever
  includes an author field in a shape the validator cannot read as the creator's
  own id, delivery becomes `unknown` rather than confirmed: the live-pilot
  preflight must confirm the real response shape (a fail-closed trade, not a
  silent risk).
- An external website/app send after the final history read is an unavoidable
  remote race, so the first live pilot requires exclusive operator use of the
  test chat. This code does not claim exactly-once delivery or atomic remote
  takeover.
- Cross-process admission uses SQLite write transactions (`BEGIN IMMEDIATE`) as
  its ordering boundary and is SQLite-only by design. On an unsupported backend
  an ENABLED bridge refuses rather than silently degrading. A local writer whose
  provider outcome is UNKNOWN (post-send exception, or a crash mid-POST) keeps a
  durable `unresolved` marker that admission honours until an operator runs
  `reconcile-writes` for that fan: this is deliberately fail-closed, so an
  uncertain legacy send can block the bridge for that conversation until it is
  reconciled. A genuinely remote writer is still outside the fence.
- Fastt's application imports `llm_models`, which cannot be imported below
  Python 3.12; the Python 3.11 test harness stubs the automation hooks. The
  live-pilot preflight must confirm the application's own runtime before
  enabling anything.

### Live-pilot runbook (draft - not executed)

1. Preflight: confirm the Python 3.12+ runtime, one designated account, one
   allowlisted test fan, an explicit activation cutoff, a positive
   `TWIN_BRIDGE_SEND_LIMIT`, and that no other device or automation will touch
   the test chat.
2. Start Twin's loopback adapter and confirm it is reachable; keep sends off.
3. `run-once` with sends off until a draft looks correct (`cli status`).
4. Enable sends with `--enable-send --send-limit <n>` and run `run-once`.
5. Stop conditions: any `delivery_unknown` or `attention` conversation - pause,
   inspect, and resolve by hand. Never re-run a dispatch for an unknown attempt.
6. Stop: `pause` the conversation, then stop the adapter. Real account ids,
   fan ids and credentials stay unset in this document.

## Windows test commands

```bat
rem once, from the repository root
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

rem then, from the repository root
set TEMP=%CD%\.tmp-test
mkdir %TEMP%
service\run_tests.cmd
service\run_tests.cmd twin_bridge_intake twin_bridge_turns twin_bridge_delivery ^
    twin_bridge_client twin_bridge_contract twin_bridge_roundtrip twin_bridge_runner ^
    twin_bridge_ownership
```

`twin_bridge_ownership` holds the Stage 1C part 2 repair regressions: durable
ownership across a restart, every real write entrance, the attempt-bound dispatch
token, the atomic admission token, the barrier-controlled takeover race, the
shared per-fan lease, the existing cooldown, the manual takeover seam,
obsolete-work cancellation/tombstoning on resume (with sent evidence preserved),
crash recovery (both windows), strict provider/history evidence, plain-text
escaping, the small-page AND 500+ burst boundary, and the documented CLI entry
point including explicit-invalid-cap refusal.

`service\run_tests.cmd` is the Windows equivalent of `service/run_tests.sh`: it
runs the named `service\tests\test_*.py` suites with the repo-root venv and
applies the "exit 0 AND a line containing all cases passed" rule. Python's exit
status is captured BEFORE `findstr` runs, so a suite that prints the marker and
then fails is reported as a failure and makes the runner exit non-zero;
`test_twin_bridge_runner.py` proves that with temporary probe suites it creates
and removes itself.

Every suite uses synthetic accounts and fan ids, an isolated temporary SQLite
file, stubbed automation/webhook/cache hooks and a stubbed model. The
cross-language contract suite and the full round-trip suite start Twin's real
loopback adapter on an ephemeral port (needs Node and Twin's `node_modules/tsx`)
and stop it in `finally`; the round trip additionally fakes only the lowest
OnlyFans HTTP layer. If the environment forbids loopback sockets or asyncio, the
suites must be run outside that sandbox; report the restriction rather than
skipping silently.
