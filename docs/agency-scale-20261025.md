# Message capacity rollout

Scope: concurrent fan replies, explicit delivery limits and synthetic capacity measurements. AI activation remains manual. Proxy sourcing, proxy allocation and IPRoyal integration belong to the separate proxy workstream. Public agency signup is out of scope.

## Required inputs
- Monthly budget for additional AWS capacity and daily model spending limits before increasing paid resources.
- Ten creator sessions for live acceptance, enabled manually by their managers.
- Measured model and provider throughput. Synthetic tests cannot establish live OnlyFans or OpenRouter capacity.

## Coordinated release
Schema revision 0092_delivery_scale preserves current limits, usage, queued work and cooldowns while allowing fractional send intervals. The schema chain must be reconciled with the separate proxy migration before integration. API, worker and supervisor must be released together after tests. Preserve current Moxy isolation and automatic imports.

FASTT_MAX_CONCURRENT_FANS defaults to 1 and permits 1..64. Increase after validating worker resources, model quotas, DB connections, delivery and takeover behavior. Turns are keyed by fan. Existing leases/claims and send admission remain authoritative. Engine activation is task-local. In-flight tasks drain before transport changes and shutdown.

Manual delivery controls accept intervals from 0.1 seconds and total allowances up to 1000000. Existing live settings and used units are retained. Allowances remain finite and never renew automatically; model spending limits still apply. These settings do not prove provider throughput.

## Evidence
CI profiles ten accounts with 50/500/1000 fans each (500/5000/10000 total), deterministic offered traffic of 100000 inbound messages/hour, a six-second synthetic model and 0.2-second fake provider. Reports include delivered and unanswered messages over 30 seconds. They exercise the runner, ledger and PostgreSQL, but do not measure the full prompt/media work or live model/provider capacity. Complete an unscaled production-like benchmark and approved delivery canary before claiming the SLA.

Capacity acceptance now uses an unscaled clock, an independent arrival writer and scheduled arrival timestamps. The JSON report records scheduled, generated and persisted counts plus ingestion delay. A 30-second drain window follows the last arrivals. The workflow fails for missing traffic, ingestion more than one second late, missing/late replies, throttle violations or harness/service errors. A successful process alone is not capacity evidence. Existing failed profiles are diagnostic only. Passing this short synthetic test remains insufficient for production release; sustained media/PPV, failure recovery and compatible rollback acceptance are outstanding.

## Verified worker starvation and isolated I/O
Corrected run 40181909426 generated and persisted its full offered workload but failed reply acceptance. The diagnostic run 40183402260 showed all six default executor threads waiting inside Gateway.send, while the corresponding loop coroutines waited for that same executor at pacing/physical-post boundaries. A two-worker reproduction started two real Gateway.send callbacks and completed neither.

The candidate fix gives native-pilot history reads, manual hold checks, media checks and physical sends a separate bounded eight-thread I/O pool, preserving ContextVar scope. Other bridge callers keep their existing executor. It changes no delivery policy or retry rule. New tests cover saturation, account-context separation, manual pause refusal and exception propagation without retry. Upgrade/fallback and production acceptance remain pending; all changes are draft only.

Target benchmarks are now explicit workflow-dispatch runs. Pushes run functional checks plus the short wait-diagnostic workload; a diagnostic job completing is not capacity acceptance. Profiling stores stack locations without locals, credentials or message bodies. The observed one-extra-arrival-per-account floating-point boundary was corrected and gets an exact 7,500-arrival schedule regression.

## Latest measured limits and release status
The full benchmark on 3860ad5 (run 40183944850) generated and persisted exactly 7,500 incoming messages per profile, at 100,000/hour during the arrival window. It failed every profile: 500 total fans produced 977 sends and left 5,304 incoming messages unanswered; 5,000 fans produced 1,833 sends and left 5,079 unanswered; 10,000 fans produced 1,782 sends and left 5,495 unanswered. Inputs can be coalesced into one reply, so send counts and answered-input counts differ. Ingress also fell behind. These figures are diagnostic test outcomes, not production capacity measurements.

The later short diagnostic on 27a6064 (run 40184818197, 10 creators and 5,000 fans) persisted 1,670 incoming messages and sent 793 replies, with no budget-refused or throttle-abandoned events. It still had substantial backlog and up to 7.122 seconds of late ingress. There is no target-SLA pass.

The candidate now serializes the first daily budget reservation on the account configuration row before the daily cost row exists, retaining the same cap. Fan generation remains concurrent, while child gateways share an account-local admission lock; final durable send checks still apply. Serial fallback retains the original worker path. PostgreSQL race and populated migration-preservation tests passed in run 40185324302, together with the targeted schema/manual-activation/isolation regressions, 705 assembled service checks and 77 engine checks. Remaining work includes event-loop/database bottlenecks, sustained media/PPV and fault acceptance, production-like process topology, paired dashboard source access, and coordinated migration/release rehearsal.

## Repeat acceptance and process topology
The repeat full benchmark on 4f48619 (run 40186436219) still failed all three profiles. All offered and persisted exactly 7,500 inbound messages. For 500 total fans it answered 6,619 inputs, left 881 unanswered and had 6,461 inputs late or unanswered. For 5,000 fans it answered 2,593, left 4,907 unanswered and had 7,246 late or unanswered. For 10,000 fans it answered 3,027, left 4,473 unanswered and had 7,168 late or unanswered. Maximum delivered-reply latencies were 286.3, 273.2 and 262.0 seconds respectively. These are failed synthetic measurements, not a current-production throughput limit.

The supervisor creates one OS process per active creator. The original harness placed ten creator services in one process, sharing its executor and event loop. Its CPU/RAM figures cannot size the production fleet. The new fleet harness creates ten independent processes with synchronized arrivals and one disposable shared PostgreSQL database. Each child must prove its account, workload, complete arrivals and start time; missing/crashed/mis-scoped workers invalidate the result. The local-only database ownership marker prevents a child from resetting or seeding the shared fixture. The parent stops all workers before removing its freshly created database.

The fleet matrix tests 500/5,000/10,000 total fans at 100,000 inbound/hour with the same six-second synthetic model and 0.2-second provider. Reported memory covers creator processes only and excludes PostgreSQL, dashboard, API and supervisor. CI resources have no production CPU quotas. Even a passing fleet result would not establish full-media throughput or justify a hardware purchase by itself.

The real native-engine reviewed-content regression now overlaps two fans on the same creator through their model and worker callbacks. Both send one reviewed paid card, retain the exact media/preview/price terms, create separate open PPV offers and persist correct ai_upseller attribution. It uses synthetic model/provider boundaries and blocked network sockets. It validates concurrent media isolation, not media throughput at 100,000/hour.

## First fleet failure: database connection exhaustion
Run 40187988201 failed all profiles with complete 7,500-message arrivals. Worker events include TooManyConnectionsError at model budget reservation and OperationalError during service work. It sent 352/70/55 replies in the 500/5,000/10,000-fan profiles, respectively. These are failed-fixture outcomes and must not be used as sustainable capacity numbers. Aggregate creator RSS peaked around 977-980 MB on two-vCPU CI hosts, excluding PostgreSQL and other services.

The first fleet harness inherited the older harness's removal of ONLYFANS_PROCESS_ROLE. That gave each async engine SQLAlchemy's generic pool instead of production's worker pool of four connections with zero overflow. The corrected harness retains worker mode and asserts those settings in every child report. It also explicitly provisions 200 connections on the disposable test PostgreSQL; this does not alter the live database or establish that production has this budget.

The existing worker pools can theoretically open ten connections per creator (four async plus six bridge), or 100 across ten creators. Test-only ingress/observation adds at least twenty connections plus parent setup. Production additionally needs API/background/supervisor/administrative/deployment reserve. A deployment must budget total connections against the actual server limit or use tested smaller pools/pooling; increasing fan concurrency alone is insufficient.

## Live resource constraints rechecked
Read-only inspection on 2026-10-25 confirmed the host remains t3.medium with two vCPUs and about 3.83 GiB visible RAM. Its AI supervisor container and every creator child share a 384 MiB memory limit and 0.5 CPU quota. The database has a 256 MiB memory limit, 0.5 CPU quota and max_connections=50; 27 connections were present at the sample. Available host memory was about 2.2 GiB, with swap in use. These are snapshots, not load-test results.

Ten synthetic creator processes consumed approximately 0.97-0.98 GiB without the full engine, database or other services. The current 384 MiB supervisor allocation cannot accommodate that measured process footprint. A ten-creator launch needs a separately tested memory/CPU allocation and explicit aggregate database pool budget. This does not establish a final host size, and no live limits, account allowances or paid resources were changed.

The first worker-role repeat (40188778850) remained invalid for capacity: the synthetic replacement connection builder omitted the real builder's account-bound provider-access initialization, causing send-check-failed refusals before HTTP. It also recorded budget pool timeouts under the worker's three-second pool acquisition limit. The corrected fake builder now performs the same single-account role binding as the real builder; the guard itself remains unchanged. No failed-fixture result is promoted to a production throughput figure.

## Budget reservation admission queue
The account-bound repeat 40189485190 persisted every offered message but failed all profiles. Across 500/5,000/10,000 total fans it sent 44/84/110 replies and left 7,446/7,416/7,390 inputs unanswered. The worker evidence records short async-pool acquisition timeouts at budget reservation, then the existing account-wide five-minute budget hold. The budget cap was not exhausted. This is a failed synthetic run, not a sustainable production rate.

Concurrent fan services now share a per-creator asyncio lock around DailyBudget.reserve only. Waiting happens before opening a database session. The real exclusive config/daily-row locks still enforce caps across processes. Model HTTP and budget settlement happen after reservation releases this short admission lock. Separate creators have separate locks, and serial mode keeps its original call path. No budget cap, refusal/retry rule, provider guard or live configuration is weakened.

Three local admission tests cover same-creator ordering with independent-creator progress, cancellation before reservation and the original unlocked default. PostgreSQL race tests now cover four- and eight-connection pools, their three-second acquisition limit, and queued/unqueued reservation paths with both ample and restrictive caps. Fleet acceptance must be repeated before drawing any throughput conclusion.

The reservation-queue run 40190352646 improved the 5,000-fan profile from 84 sends to 2,990, but still left 3,657 inputs unanswered and failed the target. The 500/10,000-fan profiles sent 1,028/4,052 replies and also failed. Worker events now expose budget settlement failures. The account queue is therefore extended to the short settlement and uncertain-outcome transactions as well as reservations. Model HTTP remains outside the lock. Synthetic diagnostics record only fixed exception classes at these budget boundaries. The global legacy ledger write lock is retained pending a separate correctness/performance review.

## Read-only metadata contention
The all-budget-queue run 40190941023 removed budget timeout/settlement events but still failed every target profile. At 5,000 total fans it sent 4,538 replies, answered 5,794 inputs and left 1,706 unanswered; reply latency still missed 30 seconds. The 500-fan profile answered 7,499/7,500, but 5,599 were late or unanswered. The 10,000-fan profile answered 3,688/7,500. No target pass is claimed.

Profiling identified 37,750 ledger.status calls taking 383.6 aggregate seconds in the 5,000-fan profile. Both status and receipts performed pure SELECTs inside the global write transaction, unnecessarily blocking writes and other readers. Concurrent PostgreSQL mode now reads those two metadata views through the existing repeatable-read read-only snapshot helper. All write/admission/ownership/receipt mutation transactions retain their original lock. Serial fallback and SQLite retain their original path. A real PostgreSQL test holds an uncommitted legacy write while both metadata reads finish, checks that they see committed state, then verifies the committed stop is visible on the next read.
