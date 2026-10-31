# Concurrent fan delivery - 31 October 2026

## Baseline and scope

The production release record pins backend runtime source
`293b23dbdf8dd5e97f27d6c0bef8ef4710415c06`; the 31 October read-only audit found the
running sender assembled from engine v2 plus later pacing, contextual, media and caption
overlays. Claude's throughput branch `a9156c3daea7b3e68d17cee8f61ffa32a39f2fb1` was
verified against that engine-v2 line, and `293b23d` is an ancestor of `a9156c3`. The
release branch merges the deployed reliability overlays through `dd76bee` and ends its
delivery code at `6207b52`; `293b23d` remains an ancestor. This does not claim that the
historical overlay assembly was a clean single commit.

The deployed source identity is recorded in
`docs/onlyfans/astra-compatible-release-20261028/evidence/production-release.json`.

The old shape was two account-wide serialisers:

1. `native_pilot.send_gate.SendGate` allowed one holder for the whole account.
2. `Ledger._consume_allowance` refused any admission while another fan had a
   `dispatching` attempt, and `next_dispatch_at`/`_after_wire` imposed the same account
   gap on every send.

The new mode removes both account-wide single-send constraints. The legacy mode is still
the default and still uses the old gate, gap, floor and behaviour.

## Account flag and controls

`of_delivery_policy.delivery_mode` is per account:

* `gap` (default) - existing single-send mode, existing `throttle_seconds` and 20 s floor.
* `concurrent` - bounded concurrent mode described below.

Migration `0103_concurrent_delivery` is now the normal `db.schema.HEAD`; bootstrap and
`alembic upgrade head` reach it. It adds:

| column | meaning |
|---|---|
| `provider_requests_per_second` | durable token refill rate per account |
| `provider_burst` | permitted burst of provider requests |
| `provider_max_concurrent` | maximum in-flight provider requests per account |
| `conversation_min_gap_seconds` | optional per-fan spacing after a settled send |
| `provider_cooldown_until` | account-wide cooldown after a 429 or account failure |

Process ceilings are separate:

* `FASTT_PREPARE_CONCURRENCY` - model preparations (default 4).
* `FASTT_SEND_CONCURRENCY` - send gate slots (default 4). Effective provider
  concurrency is the lower of this process bound and the account's
  `provider_max_concurrent`.
* `FASTT_CONVERSATION_MIN_GAP_S` - process floor for per-fan timing (default 0).
* `FASTT_DISPATCH_LEASE_S` - in-flight slot/ownership lease (default 120 s).
* `FASTT_CAMPAIGN_BATCH` - campaign intents queued per cycle in concurrent mode (default 8).

Set one account deliberately:

```bash
python -m native_pilot.control delivery-mode \
  --account of-176774528 --mode concurrent \
  --requests-per-second 4 --burst 8 --max-concurrent 8 \
  --conversation-gap 0 \
  --actor operator --reason "controlled concurrent canary"
```

Rollback is the same command with `--mode gap`. It refuses while an intent or
`dispatching` attempt is unresolved. The old image ignores the new runtime tables and
the additive policy columns have defaults, so `gap` remains a rollback path.

## Durable rules

* `engine_conversation_lease` is keyed by `(tenant_id, account_id, fan_id)`. One reply
  sequence may advance per conversation. A held lease expires only for slot accounting;
  an unresolved delivery keeps the conversation closed until reconciliation.
* `engine_dispatch_slot` counts actual in-flight provider requests. Expired crashed
  holders stop counting, while the durable send attempt stays unresolved. A failure for
  one fan therefore cannot consume account capacity forever or stall unrelated fans.
* `engine_provider_bucket` is a durable token bucket. Refill and consume happen in the
  same short transaction as admission; no transaction is open during a model call or a
  provider POST.
* Daily fan quotas and the account rolling daily cap stay atomic. The account cap now
  counts both native receipts and manual/campaign text intents (`dispatching`,
  accepted/sent and unknown), so one path cannot race the other past the cap.
* `record_sent` releases the slot and conversation lease. `record_not_sent` releases both.
  `record_unknown` releases the slot but marks the conversation unresolved, so no retry
  and no unrelated-fan stall.
* Human takeover and account/session eligibility are still re-read immediately before the
  wire. A provider 429, an HTTP 401, or an account authentication failure is account-wide.
  A fan-specific failure is not. A 401 is settled as a definite non-delivery and the
  account records a cooldown before the fan is released.
* AI, manual, campaign and follow-up text all use the same policy row, token bucket,
  conversation lease and slot table. Campaign/manual text uses
  `onlyfans_runtime.concurrent_admission`; the native runner uses the same rules in the
  ledger.
* `OFClient` now gives each worker thread its own `curl_cffi` Session, seeded with the
  same captured cookies and sticky proxy. It does not create a second login or proxy.
  SQLAlchemy pools were raised through `FASTT_DB_POOL_SIZE`/`FASTT_ASYNC_POOL_SIZE`.
* Concurrent delivery admission and per-fan control projection use a shared
  `of_runtime` lock, so different fans do not queue behind an account-wide mutex.
  Account-level controls retain the exclusive lock, and the short policy-row
  transaction still reserves slots, tokens and daily allowance atomically.

## Simulated results

`service/tests/test_concurrent_delivery.py` uses the real Service, Ledger, SendGate,
runner send boundary and intake on a virtual clock with a fake provider and model. The
provider records actual overlapping POSTs.

Configuration: 100 fans, one creator, provider 10 requests/s, burst 10, max 8
concurrent, 2 s model latency, 120 ms provider latency.

| metric | result |
|---|---:|
| replies | 100 / 100 |
| median | 15.0 s |
| p95 | 19.8 s |
| p99 | 21.0 s |
| maximum | 21.0 s |
| maximum provider overlap | 7-8 |

The same suite covers duplicate events, multi-bubble ordering, a 429 with `Retry-After`,
a 401 account authentication failure, model timeouts, human takeover, uncertain
delivery, restart during a burst, paid cards, and three creators running at once. No
duplicate or cross-fan send was observed. The paid-card scenario checks the reviewed
media set and price for four of eight concurrent fans. The concurrent suite passes 58
checks.

These are simulated results. No live OnlyFans send, real fan, or paid-media purchase was
performed for the concurrent mode. Provider rate/burst settings must be validated on one
controlled account before expanding; the code does not assume either a 10 s requirement
or permission for 100 simultaneous requests.

## Verification commands

```bash
cd service
python tests/test_concurrent_delivery.py
python tests/test_throughput_engine.py
python tests/test_fastt_engine_v2.py
python tests/test_fastt_engine_v2_units.py
python tests/test_native_pilot_ledger.py
python tests/test_native_pilot_runner.py
python tests/test_native_pilot_content.py
python -m pytest -q tests/test_concurrent_admission_units.py
```

The legacy suites remain the rollback proof. PostgreSQL acceptance was run against the
final image on disposable databases created beside the production PostgreSQL instance:

* `test_throughput_migration_pg.py`: 21 checks, including upgrade to `0103`, downgrade
  through `0100`, and upgrade back to `0103`.
* `test_concurrent_delivery_pg.py`: 15 checks, including two fans admitted together,
  same-conversation ownership races, atomic daily caps, uncertain delivery, restart
  recovery, gap-mode rollback during an in-flight send, and shared-vs-exclusive runtime
  locks.
* `test_fastt_engine_v2_pg.py`: 14 checks.

Local verification also passed `test_concurrent_delivery.py` (58 checks), the engine-v2
scenario suite (14 checks), and the throughput engine suite (49 checks).

## Deployed verification

The final code image was built from
`6207b52829a0aa0543987fc96e0c8f09ae1a1990` and deployed to the production Docker host:

| evidence | value |
|---|---|
| image tag | `323149985112.dkr.ecr.ap-southeast-2.amazonaws.com/twin-onlyfans:concurrent-6207b52` |
| image digest | `sha256:269a4c1aac59a43475883f54c3d9d052cd4d07796b37c0944d2e9ba8ef80f257` |
| deployed revision | `6207b52829a0aa0543987fc96e0c8f09ae1a1990` |
| schema | `0103_concurrent_delivery` |
| API health | `200 {"ok":true,"role":"api"}` |
| roles | API, worker, FASTT supervisor running; zero restarts |
| environment diff | `none` for all three roles except the image and `BUILD_SHA` |

All production accounts remained in `gap` mode. The policy rows were unchanged at
`requests_per_second=1`, `burst=5`, `max_concurrent=4`,
`conversation_min_gap_seconds=0`. The live supervisor still emits the pre-existing
gap-mode `twin-controls-error` lock-timeout messages under its normal account control
traffic; this run did not enable concurrent mode, so those messages are not evidence
about the concurrent path. No live concurrent fan burst was performed.

## Real-account canary

1. Apply `0103_concurrent_delivery` and the runtime `engine_concurrency_upgrade.sql`.
2. Keep every account in `gap` and deploy the image.
3. Pick one controlled account with no unresolved attempt or active intent.
4. Keep process settings at their current defaults: `FASTT_PREPARE_CONCURRENCY=4`,
   `FASTT_SEND_CONCURRENCY=4`, `FASTT_DISPATCH_LEASE_S=120`,
   `FASTT_CONVERSATION_MIN_GAP_S=0`.
5. Enable only that account with conservative account limits:

   ```bash
   python -m native_pilot.control delivery-mode \
     --account of-176774528 --mode concurrent \
     --requests-per-second 1 --burst 2 --max-concurrent 2 \
     --conversation-gap 0 \
     --actor operator --reason "controlled concurrent canary"
   ```

6. Send one controlled text and one paid card; confirm receipt, order, price, duplicate
   absence and human-takeover cancellation.
7. Raise burst/concurrency only from observed provider 429 responses and `Retry-After`.
   Do not infer a safe ceiling from the simulated run.
8. Expand account by account. Roll back the flag to `gap` if provider throttling,
   authentication errors or unresolved receipts rise.
