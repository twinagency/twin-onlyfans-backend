# R8 report: timing, capacity, integration (LOCAL ONLY, nothing pushed or deployed)

Base 6aabad9, branch `claude/of-final-r8m-20261101`. No schema change, no migration. All load numbers are SYNTHETIC.

## Status per item
| item | status |
|---|---|
| 1 measurement | DONE. New `turn-timing` event per executed turn (`native_pilot/turn_timing.py`, wired in `Service.execute`): earliest unresolved input, claim, generation start/end, decision (first receipt created), provider first/last send (receipt updated_at), reconciliation, user-visible seconds, per-route model calls/failures/truncation/latency/tokens/cost/purpose, requeues. Reuses model-* events, ledger receipts, engine_work.waiting_since. No new table. Filler/later bubbles do not move the clock (first sent bubble ends it). |
| 2 load harness | DONE on the existing virtual-time harness (`fastt_harness`/`fastt_load`), scenarios in `tests/fastt_r8_load.py`. SQLite ledger there; PG used for the Store claim path and the retained PG suites. See "Limits". |
| 3 fixes only if measured | 2 defects reproduced and fixed; 1 finding left open (below). |
| 4 gap vs concurrent | DONE in harness, no production mode change. |
| 5 capacity statement | DONE below, with caveat. |

## Measurements (virtual seconds, earliest unresolved input -> first reply; seed 7)
gap = production default (20 s per account). concurrent = shipped policy defaults of the opt-in mode (1 req/s, burst 5, 4 in flight). fast = test-suite allowance (10 req/s), synthetic only.
| scenario | mode | eligible turns | p50 | p95 | p99 | max | within 180 s | stranded | dup |
|---|---|---|---|---|---|---|---|---|---|
| 100 simultaneous fans, 1 creator | gap | 100 | 1007.7 | 1887.7 | 1967.7 | 1987.7 | 9.0% | 0 | 0 |
| same | concurrent | 100 | 53.7 | 97.7 | 101.7 | 102.7 | 100% | 0 | 0 |
| 5 creators x 40 simultaneous | gap | 200 | 407.7 | 749.1 | 788.3 | 789.1 | 22.5% | 0 | 0 |
| same | concurrent | 200 | 23.7 | 41.4 | 42.9 | 43.4 | 100% | 0 | 0 |
| 50-creator roster, ~150 inbound/min fleet (3/min/creator), 8 min | gap | 1128 | 37.4 | 126.1 | 170.9 | 352.5 | 99.3% | 0 | 0 |
| same | concurrent | 1181 | 10.1 | 12.7 | 15.5 | 20.3 | 100% | 0 | 0 |
| bursts (3 msgs 2-4 s apart, 2 bubbles, 60 fans) | gap | 60 | 1193.7 | 2213.7 | 2313.7 | 2333.7 | 8.3% | 0 | 0 |
| same | concurrent | 60 | 65.7 | 118.7 | 123.7 | 126.7 | 100% | 0 | 0 |
| multi-bubble (2 bubbles, 40 fans) | gap | 40 | 767.7 | 1467.7 | 1527.7 | 1527.7 | 12.5% | 0 | 0 |
| same | concurrent | 40 | 39.7 | 73.7 | 80.7 | 80.7 | 100% | 0 | 0 |
| media (10 of 40 get a reviewed paid card) | gap | 40 | 407.7 | 747.7 | 787.7 | 787.7 | 22.5% | 0 | 0 |
| same | concurrent | 40 | 23.7 | 40.7 | 42.7 | 42.7 | 100% | 0 | 0 |
| backlog (150 waiting + 150 new over 20 min) | gap | 224 answered+stranded | 1607.7 | 3011.7 | 3035.7 | 3043.7 | 4.0% | 62 at cut-off | 0 |
| same | concurrent | 296 | 19.7 | 138.7 | 149.7 | 171.7 | 100% | 0 | 0 |
| provider 429 x3 mid-burst (Retry-After 30/60/none), 40 fans | gap | 40 | 557.7 | 897.7 | 937.7 | 937.7 | 5.0% | 0 | 0 |
| same | concurrent | 40 | 23.7 | 126.7 | 128.7 | 128.7 | 100% | 0 | 0 |
| auth failure (401) then reconnect at +120 s, 20 fans | gap (after fix) | 20 | 518.3 | 678.3 | 698.3 | 698.3 | 0% | 0 | 0 |
| same | concurrent | 20 | 324.5 | 332.5 | 333.5 | 333.5 | 0% | 0 | 0 |
| worker killed at +50 s and restarted, 40 fans | gap | 40 | 407.7 | 747.7 | 787.7 | 787.7 | 22.5% | 0 | 0 |
| same | concurrent | 40 | 23.7 | 40.7 | 42.7 | 42.7 | 100% | 0 | 0 |
| manual takeover of 8/40 mid-burst, 1 handed back | gap | 33 | 327.7 | 607.7 | 647.7 | 647.7 | 27.3% | 0 | 0 |
| same | concurrent | 33 | 19.7 | 33.7 | 217.7 | 217.7 | 97.0% | 0 | 0 |

Notes: the 217.7 s concurrent max is the handed-back fan, whose input waited ~200 s while a person owned it (held time is inside the figure; operator-owned fans are excluded from "stranded"). The backlog gap row is overload (offered 300 turns vs 9 per 180 s capacity): the queue grows linearly, nothing is lost or duplicated, 62 turns were still waiting at the cut-off. Concurrent "fast" (10 req/s) and the 50-creator concurrent run show the same shape with lower latency. Raw numbers: `python tests/fastt_r8_load.py <scenario> --mode all --json out.json`.

## What reproduced, what was fixed
1. FIXED. Oldest need served last. `pending_fans` orders by each fan's NEWEST unanswered message, and the tick started work in that order. A fan who keeps typing (earliest input oldest) started behind every quieter fan: 1147.7 s vs 87.7 s after the fix, same total drain (60-fan backlog, gap mode). Fix: `tick_all` starts ready fans ordered by earliest unresolved input (`ready.sort`, stable). Test `test_r8_oldest_need_first.py`.
2. FIXED. A reply waiting at the send gate when the credential changed (also runner-lease loss, stop, takeover cancel) left through `SendGate.acquire`, which does not set `gateway.stopped_reason`; the cancel was reported as `native-turn-failed`, the input marked failed and the fan was never answered (2 of 20 fans stranded after auth failure + reconnect). Fix: `execute` names the real `cancel_reason`, so the existing release/backoff path gives the claim back (nothing was on the wire). Test `test_r8_cancelled_waiter_not_abandoned.py`. Takeover cancels now release rather than abandon, matching what the same condition already did when it surfaced through `stopped()`.
3. NOT REPRODUCED (Store/PG claim path, `onlyfans_runtime`): continuous input never moves `due_at` past `first_input_at + 30 s`; overdue work is due immediately; a chatty fan whose claim is invalidated every round and two quiet fans are each served once per round. Tests in `test_onlyfans_capacity_pg.py` (`test_r8_*`).
4. NOT REPRODUCED: configured 25-90 s pacing (`pacing.prepare`) is anchored on the first received input and capped at first+20 s for the quiet window; later input cannot extend it. Not load-tested (pacing flag off in the harness schema).
5. OPEN (not changed, needs a decision): after a 401 the account is blocked 300 s (`account-auth-failed`); a credential change at +120 s does not release it, so work waits ~320 s. This is a deliberate safety backoff; clearing it on a changed connection revision is a small follow-up but touches auth semantics.

## Gap vs concurrent (item 4)
Same scenarios and seed, only `delivery_mode` differs. Both modes: zero duplicates, zero cross-account, per-conversation bubble order preserved (checked per fan), nothing stranded, 20 s gap intact in gap mode (`test_r8_gap_vs_concurrent.py`). Gap mode cannot serve more than 9 simultaneous fans inside 180 s (180/20); concurrent defaults serve 100 inside ~103 s. No production mode change is proposed or made; enabling concurrent is a per-account opt-in decision.

## Capacity statement
In the synthetic harness: gap mode (20 s) meets the 95%-within-180 s gate only while an account receives well under ~9 replies per 3 minutes; a 50-creator roster at 3 inbound/min/creator measured 99.3% within 180 s (p95 126 s, max 352 s). Anything bursty above that (100 simultaneous fans, bursts, backlogs) misses the gate in gap mode by arithmetic, not by defect. Concurrent mode with the shipped policy defaults met the gate (100%) in every scenario except forced auth failure (the 300 s safety block) and the operator-held fan. No stranded due turns, no duplicate sends, no unbounded retries in any run after the two fixes.
**Caveat: a synthetic allowance is not live provider capacity.** The 1 req/s and 10 req/s allowances, 0.12 s provider latency, lognormal model latency (median ~2 s) and the absence of real OnlyFans rate limiting, proxy latency and model-provider tails are assumptions of the harness. No real provider accepted any of these rates. The harness runs one process with SQLite and virtual time; DB contention is only measured by the retained PG suites. Do not quote these numbers as supported production load.

## Tests run (all offline; PG = own container ofpg-r8m, port 55441)
- `pytest tests/test_turn_timing.py` 2 passed
- `pytest tests/test_onlyfans_capacity_pg.py` 7 passed (5 retained + 2 new)
- `pytest tests/test_r8_oldest_need_first.py tests/test_r8_cancelled_waiter_not_abandoned.py tests/test_r8_gap_vs_concurrent.py` 3 passed
- script-style, after the final code change: test_fastt_engine_v2 74 checks; test_concurrent_delivery 58; test_native_pilot_service 768; test_throughput_engine 49; test_fastt_engine_v2_units 57; test_throughput_daily_cap 15; test_throughput_burst (BURST_N=100) 22. All passed.
- `tests/test_native_pilot_load_pg.py` (PG, scaled real clock): FAILS, and fails identically on base 6aabad9 (no reply inside the tiny window on this loaded machine). One real harness drift was found and fixed (the SyntheticEngine stand-in rejected `open_offers_ok`/`reply_format`, turning every turn into native-turn-failed); turns now run (turn-timing events appear) but no send lands in the window. Not resolved; not caused by R8 code.
- Not run: the full PG module sweep and the full non-PG suite (time); `fastt_load.py` stock profiles.

## Open risks
- Harness is single-process SQLite virtual-time; the FASTT ledger is not exercised on PG at load here.
- `ready.sort` relies on `burst_state` first-input strings (same `_dt` format); rows without a burst row keep their old place.
- Cancel-reason mapping now releases takeover-cancelled inputs; release_input still refuses if any send attempt stands.
- Fixture change in `fastt_harness.py` (skip_list gets `added_at` + primary key) mirrors the production column the engine writes.
- Auth block finding (5) and the pre-existing red PG load test are unresolved.
