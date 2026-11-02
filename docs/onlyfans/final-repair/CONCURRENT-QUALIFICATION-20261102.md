# Concurrent reply qualification, 2 November 2026

Status: isolated implementation and measurement only. **Not release-qualified. No merge, deployment, production setting, activation, purchase or customer message.** The consolidated brief, qualification checkpoint and R9 gates remain authoritative. The earlier 17 failed PostgreSQL modules remain failed in their frozen historical report. Moxy is excluded.

Implementation commit: `cfab868e2c87706ddce4ff6cfb2bbeffd76c720f` (parent `d3a702262abd918588587c12f67c5fb9611100d7`). Evidence-only follow-up commits do not change runtime source.

## Source and preservation

Remote branch `codex/of-original-completion-20261102` was re-read at `d3a702262abd918588587c12f67c5fb9611100d7`. Changes are published with that actual remote parent and an expected-head lease, not the reconstructed local Git ancestry. The previous unfinished checkout `/workspace/scratch/9a82163d809e/backend`, its repair snapshots, and the laptop qualification directories were left intact. Existing documents/evidence are retained. The final source manifest and each run's start hashes distinguish frozen execution from intermediate diagnostics.

## Changes and trace

An eligible persisted input is selected by `pending_fans`/burst selection, assigned a bounded preparation slot and an exclusive fan child, fenced by native pilot and bridge ownership, scheduled by existing pacing, read into NativeEngine/ai_chatter context, generated through NativePilotModel, admitted through the existing SendGate plus shared SQL policy/bucket/slots, rechecked at the wire by OFClient, validated, recorded as a receipt and projected/attributed before the claim completes. No alternate engine or sender was introduced.

* Batched the read-only unchanged-controls check. Only missing/drifting fans enter the existing locked reconciliation; authority and final wire checks remain. Baseline Windows `_twin_controls` spent 93.09 aggregate seconds over 88 ticks; the 67.1-second candidate run spent 6.47 aggregate seconds over 97 ticks. These cross-host numbers identify repeated work, not a controlled speedup ratio.
* Moved PostgreSQL lock-waiting admission, ownership, lease, audit, claim, model-count and settlement operations off the async loop. Metadata status/receipt reads no longer take the account writer advisory lock. Lock owners and heartbeats can progress while admission waits. Account admission transactions and fan ownership remain exclusive. SQLite's existing execution contract and other coordination callers retain their original default behavior.
* Isolated mutable content/quote state per fan. Shared plans previously let another fan clear a quote or commercial hold. Vault plans bind the actual fan.
* Released preparation slots until a durable pacing due time. Waiting fans previously repeatedly occupied the oldest preparation slots.
* Reserved SendGate conversation keys while queued and during handoff, including cancellation cleanup. Refused admission no longer consumes a bubble number. Only rolled-back SQL contention is retried; uncertain POSTs retain reconciliation.
* Prevented out-of-order callers moving the shared token-bucket timestamp backwards and granting elapsed time twice. Both native and competing-sender admission use the same calculation.

The existing concurrent branch in `ServiceGateway.pace` uses cooldown, provider token wait, conversation gap and shared concurrency instead of `next_dispatch_at`/the 20-second account gap. Gap mode still uses its floor. Preparation default remains 4 (ceiling 16); send default remains 4. No new workers multiply the rate budget. Tests explored preparation 8/16 and sending 8, with explicitly synthetic 10 requests/sec and maximum concurrency 8. Preparation 16 was slower; there is no evidence to raise its ceiling or use 100 workers.

## Environment and fidelity

Python 3.12.14, pinned qualification dependencies, Linux, 8 CPU cgroup quota, 8 GiB memory. Disposable PostgreSQL 16.15, loopback only, trust-authenticated synthetic role, separate UUID databases, shared_buffers=128MB. The interrupted/reused-cluster `complete-evidence` attempt lost its PG data-directory lock file and is excluded as infrastructure failure; its logs are retained. Final `sealed-evidence` uses a newly initialized dedicated cluster directory. Initial runs use max_connections=100; `compat-*` reruns use 300 to test the measured 50-creator connection ceiling. The final `complete-50x2-*` simulation uses a declared aggregate async pool of 64, overflow 0, timeout 10s, with the same shared admission controls. This replaces only the fleet fixture's shared SQLAlchemy engine/session factory through its public API, retaining real sessions and queries. It is not new send workers. The preceding worker-role fixture hit history-unreadable refusals (0/100), which are retained as failed fixture diagnostics; that result is not claimed as capacity. The bound accommodates 100 active fans while leaving headroom under 300 server connections (100 control-store idle connections + 64 async + 24 raw store + fixture/model connections). The earlier pool-exhaustion runs remain failed. The roster simulation places N real creator Services in one process with an aggregate bounded thread executor; production uses separate creator processes. It is not a proof of 50 production containers or their aggregate memory.

The managed executor maps only one UID. PostgreSQL alone used the retained test-only ownership shim (`qualification_uid.c`) to emulate a non-root UID. PostgreSQL query, transaction, lock and recovery code was unmodified. This is an isolated functional/capacity environment, not deployment-image or OS-user parity. Earlier overlay-backed PostgreSQL encountered kernel/overlay EOF corruption and is excluded; measured runs use a fresh tmpfs cluster. Persistent storage/fsync durability and production crash-durability are not established by tmpfs.

All model and provider HTTP is synthetic. NativeEngine, ai_chatter context/database work, model adapter and durable spend/call reservation, orchestration, ownership, admission, native protocol, receipts and attribution are real. Synthetic model latency is 0.2s, POST latency 0.12s, history latency 0.02s, with explicit slow/error overrides. No real token-generation throughput, reply quality or native provider capacity is demonstrated. No live capacity probes occurred. Environments were scrubbed, dotenv disabled and non-loopback Python sockets rejected. One real Intake.handle/transcoder/dedupe sample is included; the simultaneous burst otherwise persists inputs in one transaction. This is not an actual WebSocket/fan ingress load test.

Clock is unscaled wall time. Latency starts before persistence at the earliest unanswered eligible input, not model start. First substantive synthetic response latency is counted only for positively confirmed receipts; full-reply latency includes terminal claim persistence. Unfinished turns remain in the denominator and have right-censored ages. Successful-only percentile columns must be read with the completed/eligible column. There are no customer IDs or content in the fixtures.

## Measurements

All rows below use synthetic model/provider HTTP. Rate is per creator and remains shared by competing senders. Full metrics, stage distributions, outstanding work, CPU/RSS, connection/lock peaks, commands and source hashes are in [the evidence index](evidence/concurrent-20261102/index.json). Compressed JSON files retain raw stage and lock samples. Exit zero means measurement collection finished, not that a timing/correctness gate passed.

| Run | Complete / eligible | Full complete | elapsed s | first p50 / p95 / p99 s | <=180 share | CPU one-core % | RSS MiB | DB peak |
|---|---:|---:|---:|---|---:|---:|---:|---:|
| final-20-429 | 20/20 | 20 | 33.741 | 14.4583 / 23.2425 / 33.6775 | 1.0 | 47.99 | 182.34 | 29 |
| final-100-default | 100/100 | 100 | 102.016 | 51.9786 / 96.9674 / 100.9309 | 1.0 | 61.42 | 226.49 | 23 |
| final-20-401 | 17/20 | 17 | 50.032 | 13.3724 / 21.2003 / 21.2003 | 0.85 | 28.52 | 181.44 | 28 |
| final-50x2-concurrent | 85/100 | 85 | 190.047 | 101.2119 / 109.6525 / 173.6182 | 0.85 | 70.26 | 334.73 | 100 |
| final-3x20-verify | 60/60 | 60 | 52.251 | 39.7756 / 51.9017 / 52.1583 | 1.0 | 95.12 | 225.74 | 47 |
| final-20-timeout-accepted | 19/20 | 19 | 90.016 | 14.2094 / 24.6689 / 24.6689 | 0.95 | 19.08 | 177.51 | 28 |
| final-20-multi-slow-held-competing | 19/19 | 19 | 41.415 | 19.3191 / 35.3103 / 35.3103 | 1.0 | 32.14 | 175.79 | 26 |
| final-100-gap | 10/100 | 10 | 190.044 | 87.1413 / 188.0403 / 188.0403 | 0.09 | 7.16 | 166.55 | 19 |
| final-100-paced | 100/100 | 100 | 131.368 | 81.3705 / 126.3564 / 130.3014 | 1.0 | 54.14 | 231.46 | 31 |
| final-20-media | 20/20 | 20 | 40.107 | 20.9848 / 39.0363 / 40.0274 | 1.0 | 34.31 | 178.8 | 26 |
| final-50x2-gap | 80/100 | 80 | 190.044 | 99.1424 / 120.664 / 189.9672 | 0.79 | 69.64 | 329.67 | 99 |
| final-3x20-concurrent | 60/60 | 60 | 55.551 | 42.9903 / 55.3597 / 55.3717 | 1.0 | 88.07 | 221.5 | 46 |
| final-20-timeout-lost | 19/20 | 19 | 60.013 | 14.2639 / 23.8884 / 23.8884 | 0.95 | 23.46 | 177.7 | 29 |
| final-100-p16-s8-r10 | 100/100 | 100 | 79.662 | 45.0098 / 78.9878 / 79.5152 | 1.0 | 94.75 | 241.23 | 36 |
| final-3x20-gap | 27/60 | 27 | 190.005 | 92.5823 / 173.3219 / 173.3234 | 0.45 | 15.7 | 202.36 | 34 |
| final-20-profile | 20/20 | 20 | 22.171 | 17.0566 / 21.8108 / 22.094 | 1.0 | 86.38 | 197.61 | 28 |
| final-100-8-verify | 100/100 | 100 | 75.289 | 42.6133 / 73.728 / 75.0366 | 1.0 | 96.34 | 233.95 | 30 |
| final-20-takeover | 19/19 | 19 | 22.636 | 13.858 / 22.576 / 22.576 | 1.0 | 60.87 | 180.88 | 27 |
| baseline-linux-100-default | 100/100 | not recorded | 102.135 | 52.0795 / 97.0877 / 101.0644 | 1.0 | 72.82 | 222.22 | 22 |
| final-100-p8-s8-r10 | 100/100 | 100 | 67.133 | 37.7603 / 65.7027 / 66.9376 | 1.0 | 95.47 | 234.46 | 30 |
| compat-50x2-concurrent | 83/100 | 83 | 190.042 | 109.8858 / 115.7215 / 116.8129 | 0.83 | 72.27 | 343.02 | 121 |
| compat-100-8 | 100/100 | 100 | 76.856 | 41.7051 / 75.055 / 76.326 | 1.0 | 95.36 | 230.85 | 30 |
| compat-timeout-accepted | 20/20 | 19 | 61.759 | 13.6561 / 23.2104 / 25.2551 | 1.0 | 22.87 | 180.38 | 27 |
| compat-50x2-gap | 95/100 | 95 | 190.042 | 105.2833 / 124.6833 / 125.6247 | 0.95 | 72.78 | 342.15 | 119 |
| complete-50x2-concurrent | 0/100 | 0 | 190.022 | None / None / None | 0.0 | 77.54 | 358.19 | 180 |
| pool-50x2-gap | 100/100 | 100 | 123.436 | 102.9801 / 122.5585 / 123.2832 | 1.0 | 94.93 | 361.71 | 183 |
| complete-50x2-gap | 0/100 | 0 | 190.05 | None / None / None | 0.0 | 72.83 | 358.91 | 169 |
| final-published-100 | 100/100 | 100 | 71.905 | 40.5897 / 70.1625 / 71.4765 | 1.0 | 95.86 | 234.02 | 30 |
| pool-50x2-concurrent | 100/100 | 100 | 114.151 | 106.106 / 109.2674 / 112.0837 | 1.0 | 101.36 | 363.31 | 181 |

## Targets, constraints and remaining gates

* 100 distinct complete replies within 20 seconds, delays disabled: **FAIL**. Fastest observed candidate was 67.133 seconds (100/100 full replies), p95 first acceptance 65.703s, preparation 8/send 8/test rate 10. The final frozen application+harness run completed 100/100 full replies in **71.905s**, first-response p50/p95/p99 **40.590/70.163/71.477s**. The fastest earlier candidate (67.133s) is not substituted for this exact final-source result. Neither is live capacity. At the retained 1 request/sec, burst 5, the bucket alone imposes at least 95 seconds for 100 single-POST turns after its initial burst; multi-bubble replies require more tokens.
* The same-host default-rate comparison was approximately 102.14s before versus 102.02s after. The provider allowance dominates that workload; do not advertise the higher-rate result as a default-setting speedup.
* Source-configured pacing, 100 fans, prepare 8/send 4/rate 1: **100/100 within 180s**, all full replies by 131.368s, p95 first 126.356s. This passes the synthetic 95% target for that workload. The source actually draws a triangular **25–90s** delay, not a full 25–180s distribution. Thus the separately requested full 25–180s configuration remains **unverified**, and changing that policy was kept outside this concurrency repair. No live 180s SLA is claimed.
* 50 creators means exactly **2 simultaneous fans per creator, 100 total**, not 100 per creator. At 100 DB connections both modes exhausted connections: concurrent 85/100 and gap 80/100 by 190s. Those are failed capacity results, not hidden infrastructure skips. The 300-connection concurrent rerun still failed: 83/100 by 190.042s, peak 121 connections, RSS343.02MiB, CPU72.27% of one core. Its single-process shared async pool (SQLAlchemy default 5+10) timed out after 30s in context/obligation reads. Raising server connections alone does not fix application pool starvation. With the same 64-connection aggregate pool, gap mode completed 100/100 by123.436s, p95 first122.559s; this 2-fan-per-creator workload is dominated by shared-process context/SQL CPU rather than the per-account gap. The corrected aggregate-pool concurrent run then completed 100/100 full replies in 114.151s, p95 first acceptance109.267s, peak181 DB connections, RSS363.31MiB, CPU101.36% of one core, zero recorded correctness errors. The only source changed during that run was the separate native-service test module; all application and benchmark bytes stayed unchanged. This passes the synthetic 180s capacity target for 50x2 with the declared resource configuration. Production still needs a fleet-wide DB connection budget/pooler and separate-process memory sizing before a 50-creator approval.
* One slow fan (20s), one operator-held fan and a competing account runner: 19/19 eligible completed, held fan received no POST, contender never activated. Manual takeover: 19/19 remaining eligible completed; no wire calls after takeover acknowledgment. Multi-bubble/media run: 20/20, 36 validated sends; canonical synthetic $3 assets [7101,7102] and preview [7101] are asserted on the wire. Original commercial/purchase regression scenarios remain separate evidence.
* 429: all 20 completed after the Retry-After fixture; existing durable provider cooldown controls retained. 401: 17/20 completed before the authentication stop, three unfinished at 50s. The retained 300s post-401 cooldown intentionally prevents an availability/SLA pass. Timeout without acceptance: 19/20, one unresolved, no blind retry. The initial accepted-response-lost fixture omitted required native history fields and stayed unresolved; that failed run is retained. The corrected native-envelope fixture recovered 20/20 positive receipts without a duplicate send in 61.759s; the recovered turn retained a stopped local input (19/20 full completion), so receipt recovery is not misreported as all claims complete. Silence never proves non-delivery.
* Existing real-PG tests cover crash-expired ownership, restarted-worker uncertain-send refusal, process death and receipt/projection recovery without a second POST. These are separate failure scenarios, not a claim that a 100-fan process-kill workload passed. A full crash-under-load qualification remains open.

The 67.1s run's stage p50/p95/p99 seconds: pickup/slot 33.229/61.438/63.899; pre-model context+ownership 3.625/4.868/4.906; synthetic model HTTP .202/.227/.359; model-end-to-wire .439/.606/.951; provider preflight .023/.036/.052; delivery-budget wait .034/.066/.071; admission SQL .085/.153/.539; provider acceptance .121/.125/.130; record_sent .028/.091/.149. Intake/transcoder/dedupe sample .127s and burst persistence .161s. These overlap and must not be summed. There were 7,308 async database statements (35.61 aggregate seconds) and 48,829 raw statements (92.80 aggregate seconds). CPU was 95.47% of one core, RSS234.46MiB, 30 connections, 4 lock waiters peak. Query/ORM/context work and serial Python execution remain material; sending is not the only bottleneck. The diagnostic cProfile also shows SQL/SQLAlchemy reflection/compilation work. No CPU-heavy engine rewrite was attempted.

## Regressions and evidence interpretation

The retained local run executes the original **43 commercial-state tests**, **26 full-path scenarios**, egress isolation, launch isolation, reply timing/format, admission/gate/ledger/engine checks. Focused PostgreSQL runs include controls, text delivery, takeover, ledger markers and ISO45 isolation. The final frozen PostgreSQL native-service rerun passes **all 800 checks** (`terminal-800-service-pg`,81.512s). Earlier intermediate asynchronous/virtual-clock fixture failures are retained rather than overwritten. The final dedicated disposable server shut down cleanly after this run. `compat-*` names identify the final compatibility reruns, including the model adapter's 15 tests and the new focused concurrency tests. The final SQLite native service passes all **776 checks**. Its virtual-clock fixtures now preserve provider projection timestamps, give zero-latency fake generation an explicit one-second input/reply ordering, and prevent each PG heartbeat from advancing shared time. PG fixture waits are slower and use one shared wall-clock-derived virtual advance, rather than independently multiplying time by fan count or freezing held retry deadlines. The harness timeout is 60s; original delivery/ownership assertions remain. These fixture clocks do not affect the unscaled capacity benchmark. The index gives actual exit codes for each version; an earlier pass is not substituted for a failed final run.

## Exact pilot proposal for separate approval

No pilot is activated. Proposed scope is one explicitly approved creator, an approved test conversation/roster and a staffed operator, after all original R9 gates including native provider limits, approved spend limits, live read-only parity and paid receipt/purchase checks. Proposed settings: `delivery_mode=concurrent`, `FASTT_PREPARE_CONCURRENCY=8`, `FASTT_SEND_CONCURRENCY=4`, `provider_requests_per_second=1`, `provider_burst=5`, `provider_max_concurrent=4`, `FASTT_AUTO_RESOLVE_ABSENT_AFTER_S=0`, existing pacing enabled (source 25–90s), existing queue/lease/cooldown/holds and per-fan controls retained. No worker-count increase. For an isolated pilot process, reserve at least one CPU core and 512MiB memory (headroom assumption above the measured synthetic RSS, not validated real-model sizing), and budget its sync 8+16 and worker async 12+8 pool ceilings within the server-wide connection allocation. Retain the existing approved model-spend cap; capture its exact live value read-only before approval. Do not approve 10 requests/sec from synthetic tests.

Available public rate documentation at https://docs.onlyfansapi.com/introduction/essentials/rate-limits describes a separate third-party gateway's team/workspace quotas. The existing OFClient sends directly to native OnlyFans endpoints. Those gateway quotas cannot establish native supported limits. No applicable native throughput guarantee was found; rate1/burst5/max4 are retained source defaults, not a verified provider contract. Smallest next action is read-only provider-supported limits/telemetry verification plus final compatibility review, followed by separately approved model/receipt/purchase checks. The 20-second goal additionally needs a supported higher allowance and measured reductions in context/SQL/CPU cost. The full 25–180-second pacing requirement needs explicit configuration reconciliation and a new wall-clock run. The existing broad-suite failures, live quality/parity, sibling contracts and rollback gates are not cleared by this concurrency result.

## Reproduction

Use an owned disposable PostgreSQL instance and the pinned qualification environment, never customer/provider credentials. Commands and all actual arguments are retained in the index and drivers. A normal non-root PostgreSQL instance does not need the managed-executor UID shim.

```bash
export ONLYFANS_TEST_PG_URL=postgresql+psycopg2://qualification@127.0.0.1:58106/postgres
export PYTHON_DOTENV_DISABLED=1
export PYTHONPATH=scripts/onlyfans_test_network:service:service/tests
python service/native_pilot/tools/concurrent_benchmark.py --name concurrent100 --fans 100 --prepare 8 --send 4 --duration 190 --out concurrent100.json
python service/native_pilot/tools/concurrent_benchmark.py --name gap100 --fans 100 --mode gap --duration 190 --out gap100.json
python service/native_pilot/tools/concurrent_benchmark.py --name paced100 --fans 100 --prepare 8 --send 4 --paced --duration 190 --out paced100.json
python service/tests/test_native_pilot_service_pg.py
python -m pytest -q service/tests/test_concurrent_path_regressions.py service/tests/test_concurrent_path_pg.py
```

Use a scrubbed environment as in the retained driver. The harness rejects non-local databases and creates/drops only its own UUID database. Baseline reconstruction is diagnostic plumbing; publish ancestry always starts at the verified remote commit.
