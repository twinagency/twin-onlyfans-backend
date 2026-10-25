# Single-creator capacity: isolated candidate, 26 October 2026

Status: tests complete. The 2,400/hour target failed. Not ready for deployment; production unchanged.

The target is one creator receiving 2,400 messages per hour across different fans, with complete replies within 90 seconds. Creator count is not a substitute for concentrated traffic.

## Candidate and unchanged behavior

The candidate starts at the verified deployed worker revision 06ee3026b80cdf9f0858d744937d0e1cf1a4ae9e. Application changes introduce bounded parallel fan tasks, isolated per-fan engine contexts, shared account budget admission, private account-bound delivery sessions, and separate bounded execution pools for delivery and local database work. A fan still has at most one active turn.

Both concurrency settings default to 1. Production allocation and delivery policies have not been changed by this work. AI activation remains manual. This candidate contains no proxy work, public agency signup, database migration, prompt edits, sales-rule edits, or changes to media-selection decisions. It excludes the earlier experiment's fractional pacing and activation allowance changes.

The verified diff leaves service/automations, native_pilot/content.py, native_pilot/twin_context.py, service/db, and onlyfans_runtime/connections.py unchanged. Engine wrapper changes isolate concurrent contexts; they do not replace the conversation engine.

## Test method

Run: https://github.com/twinagency/twin-onlyfans-backend/actions/runs/40210142227
Tested application commit: 10818235f72f81163b64c34748405ee04d9c2c7d.

- One synthetic creator; a pool of 500 synthetic fans, with distinct fans for each scheduled input.
- Real native conversation engine, prompt construction, message splitting, PostgreSQL ledger, budget and delivery guards.
- Each text response becomes two bubbles. Every fifth response is one reviewed $3 media card containing a photo preview and a video. This means 1.8 outbound messages per input.
- Simulated AI latency is six seconds per call; simulated provider history, vault lookup, and send operations each take 1.6 seconds.
- 16 concurrent fan tasks and four delivery lanes. Test-only one-second send admission interval and finite test allowance; live policies are untouched.
- 30-second startup window, ten minutes of scheduled arrivals, then 90 seconds to drain. Unscaled wall clock.
- Rates of 600, 1,200 and 2,400 incoming messages/hour, each with steady arrivals and with bursts of 30 at the same average rate.
- Python process: 384 MiB, half a CPU core, no swap. PostgreSQL: 256 MiB, half a CPU core, no swap, maximum 50 connections.
- Local disposable databases, simulated AI/provider HTTP transports, and a localhost socket guard. No real fan messages.

Acceptance requires all scheduled inputs to be persisted promptly, complete answers within 90 seconds, correct bubble counts and media terms, correct fan routing, respected pacing, no model/provider errors, and no runtime failures. A low median alone cannot pass.

## Results

The one-minute traffic smoke check at 600 incoming messages/hour passed: 10 inputs, 18 outgoing messages including two media cards, no missing or extra replies, and maximum complete-answer latency 20.93 seconds. Peak process RSS was approximately 149 MiB. This short check is not a sustained capacity result.

| Incoming/hour | Arrivals | Complete answers | Outgoing messages | Max complete answer | 90-second result |
|---:|---|---:|---:|---:|---|
| 600 | Steady | 100/100 | 180 | 30.32s | PASS |
| 600 | Bursts of 30 | 100/100 | 180 | 105.64s | FAIL |
| 1,200 | Steady | 200/200 | 360 | 111.96s | FAIL |
| 1,200 | Bursts of 30 | 200/200 | 360 | 164.67s | FAIL |
| 2,400 | Steady | 225/400 | 412 | 337.67s | FAIL |
| 2,400 | Bursts of 30 | 219/400 | 404 | 378.58s | FAIL |

Maximum latency above covers completed answers only. In the 2,400/hour steady case, 168 fans had no reply and another seven had incomplete answers at the end. In its burst case, 169 had no reply and another 12 had incomplete answers. These are failures, not excluded fast samples.

The only passing sustained case was 600 incoming messages/hour with evenly spaced arrivals. It delivered 180 outbound messages for 100 inputs, including 20 reviewed media cards, with every complete answer within 30.32 seconds. That workload corresponds to 1,080 outbound messages/hour during sustained arrivals; the report's all-runtime average includes startup and the final drain and must not be mistaken for the offered hourly rate. This is not a certified maximum and does not pass the tested burst requirement.

All six cases reported zero extra reply bubbles, no fan-marker/media-term validation errors, and no model errors. The high-rate tests failed through delay and unfinished work. Shutdown stopped pending turns; it did not make those messages successful.

Peak Python RSS ranged from 163.4 to 184.9 MiB. Python CPU averaged 3.9% to 9.2% of one core, below its 50% allocation. There were at most ten application database connections. PostgreSQL CPU was not measured separately; these figures do not exclude all database or host effects.

Profiles show native callbacks waiting for delivery, queued delivery sessions, history checks, and repeated admission attempts. At 1,200/hour steady, 653 begin_attempt calls produced 360 admissions; at 2,400/hour steady, 677 calls produced 413 admissions. The code retries after an account pacing race and repeats preflight checks. This supports focusing further investigation on delivery scheduling and pacing rather than buying RAM. It does not prove a replacement scheduler safe.

The interval check measures ledger admissions. Physical HTTP timing can differ: the shortest recorded simulated wire gap was 0.70 seconds. The test therefore does not certify a strict one-second physical send interval or provider-approved rate.

Passing regressions included 702 existing service checks, 77 engine checks, real reviewed-card and concurrent fan-attribution checks, the full PostgreSQL storage/ownership suites, 721 PostgreSQL service checks, and the concurrency/budget/acceptance tests. The overall workflow is correctly red because five of the six capacity cases failed.

Machine-readable summary: [single-creator-2026-10-26.json](single-creator-2026-10-26.json).


## Boundaries

This measures the candidate's behavior under an explicit synthetic workload; it does not certify live OnlyFans rate tolerance, real model latency, live intake reliability, long conversation histories, multiple live creators sharing the host, binary media uploads, vision, the full vault resolver, or Twin's live persona/context integration. The test fixture does not exercise repeated incoming messages within the same active fan turn. Existing grouping and interruption behavior is preserved.

At one outbound message per second, the theoretical long-run ceiling is 3,600 outbound messages/hour before other work. This workload needs 4,320 outbound messages/hour at 2,400 inputs/hour. More RAM does not remove that arithmetic limit. The live 20-second interval has a much lower ceiling and remains unchanged.

## Rollback and release constraints

The existing PostgreSQL tests check ownership, restart behavior, uncertain-send handling, ledger admission, and compatibility with the original 0091 schema release. These passed, alongside concurrency and existing engine regressions. No schema migration is introduced.

A future release must be built against the then-current deployed source, account for concurrent work in other branches, validate actual provider/AI timing and authorized pacing, and set a deliberate finite delivery/model budget. This branch is not a live capacity upgrade.

Rollback would stop new admissions, drain or preserve in-flight outcomes, restore the prior image and concurrency configuration, and retain receipts and consumed budgets. Do not clear delivery-unknown outcomes or replay them automatically. A rollback can pause replies; these checks do not establish zero downtime or zero risk.

## What is required next

The next engineering stage is a separately reviewed delivery-scheduling change that addresses queue fairness, admission retries and physical send pacing while retaining fresh-history checks, per-fan ordering, manual takeover, finite budgets and uncertain-send handling. It needs its own burst and steady regression results before any live rollout.

The 2,400-input target also requires enough authorized outbound rate for the unchanged answer shape. With this fixture's 1.8 outgoing messages/input, one-second admission spacing cannot sustain the target even with perfect scheduling. A rate change must be explicit and verified against the actual provider; it cannot be replaced by adding RAM or by silently shortening the bot's replies. The live finite delivery allowance and model budget also require deliberate operator configuration before sustained use.

No passing result here establishes a universal maximum of 90 seconds. Repeated incoming messages, external slowdowns, outages, and larger bursts remain outside this result.
