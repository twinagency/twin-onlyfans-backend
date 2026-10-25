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
