# Parallel history capacity, 1 November 2026

Goal: full available history and current memory for every fan, with a measured 90-minute onboarding target. No history cutoff or model downgrade is introduced.

## Status and integration

Prepared against `7616ec92bf01a6b7f3beb45bd45b943309c17285` (PR #75), which contains the archive parsing/queue repairs. This is a draft capacity release. Reconcile with the actual live worker overlays and other ongoing work before deployment. Do not deploy this entire historical base over newer conversation-engine changes. The live database already has `0105_earnings_totals`; capacity is reconciled as `0106_parallel_history_reads` and retains the earnings model/migration. The migration uses Alembic frozen constraint names to avoid naming-convention expansion.

Production has not been changed by this work. The last connection check showed the laptop/server access route offline. The profile learner was stopped at the last production audit; its current credentials, budget, and service state must be checked before declaring memory restored.

## Software capacities

* Per-account configured HTTP read budget: 1–64 requests/second; up to 128 requests in flight. These are implementation bounds, not an OnlyFans allowance.
* A native page operation performs TWO HTTP requests: identity and page. Enhanced admission charges two tokens per operation. Connection checks consume one. The legacy default retains its historical one-token-per-page-operation accounting.
* One provider page operation holds one durable lease across both HTTP calls. All lanes share backoff and rate capacity. Background work leaves separate concurrent slots for live inbox and financial reads. Existing session, creator/binding, scope, cursor, and evidence checks remain.
* A backoff signal halves the effective shared rate and sets the provider retry deadline. Other successful requests cannot clear it. Rate recovers by one request/second after each five-minute successful recovery interval. This is conservative for transient errors as well as 429s.
* Dedicated archive executor: 1–256 total worker threads, default 64. This bound is shared across configured creators; it does not grant 256 threads to each creator.
* Archive pages start at 20, grow to 40/80/100 after successful page commits, and halve on an explicit response-byte overflow, down to 5. The cursor never advances on an oversized response. Other parser/transport errors keep their explicit retry state.
* Memory learner: 1–128 concurrent jobs, default 2. Independent consumers avoid waiting for the slowest call in a batch. PostgreSQL work runs off the async event loop; DB connections remain bounded at 16. The selected model, extraction and evidence checks, and financial budget are preserved.
* Profile seeding: configurable 1–10,000 fans/account/pass, default 500. A 5,000 setting removes the artificial 48-minute queue-enrolment floor for a 24,000-fan creator.

## Rollout

1. Record live source/image/schema/configuration and reconcile changes. Keep one deployer. Capture the current history/message/memory counters for all accounts. Confirm migration prerequisites and test the upgrade on a disposable PostgreSQL copy. Never use production credentials or source messages in CI.
2. Build/test the integrated image first. Run the dedicated PostgreSQL capacity workflow, existing history/provider-budget PostgreSQL tests, and the profile learning regressions. Its SQLite tests alone are insufficient evidence for PostgreSQL row locking.
3. Drain read/import/learner workers, preserving all state. Apply migration `0106_parallel_history_reads`; run structural schema checks. Deploy the same new rate-budget implementation and capacity map to EVERY reader (worker, independent inbox/ledger/content processes, dedicated importer). Restart those read services. The new importer is a separate service; do not start or enable a reply supervisor as part of this operation.
4. Configure Maile only for the first measurement. Example initial capacity map: `ONLYFANS_IMPORT_CAPACITY={"of-323358988":{"rps":2,"inflight":8}}`. All workers must agree on this map. Start `python -m onlyfans_runtime.parallel_history` with `ONLYFANS_PARALLEL_IMPORT_ENABLED=1`, the existing worker role/tenant/DB/connection-key settings, `ONLYFANS_IMPORT_WORKERS=64`, and a supervised restart policy. No new credentials or session are fabricated.
5. Preflight each account's learner model ID/key, provider RPM/TPM, remaining credits, daily cost cap, and reply budget reserve. Reconcile estimated onboarding spend with the existing cap; report a budget blocker rather than silently increasing it. Start the supervised profile learner at `ONLYFANS_PROFILE_CONCURRENCY=16` and `ONLYFANS_PROFILE_SEED_BATCH=5000`. Raise concurrency through 32/64/128 only as latency, DB load, provider quotas, and actual cost permit. Keep live-reply budget capacity reserved.
6. Increase Maile's read budget through 4/8/12/24/48/64 HTTP requests/second, recording at least a stable five-minute window at each stage. Do not increase while a shared hold, authentication problem, rising error rate, growing live-inbox/ledger lag, DB timeouts, or resource exhaustion remains. Measure real outbound HTTP calls: two per page, plus discovery/ledger/other reads. Record successful archive pages/sec, messages/sec, memory receipts/sec, model token/call rate, p50/p95 latency, 429s/retry time, CPU/RAM, DB lock/pool waits, and live inbox/purchase freshness. The effective governor may be below the configured ceiling after backoff.
7. Choose the highest stable rate with at least 25% capacity margin. Then repeat with multiple creators simultaneously, checking both per-account and any shared proxy/provider/model quota. If a provider ceiling prevents the target, record it and investigate a supported bulk historical import or a provider with demonstrated account-level throughput. A third-party workspace RPM advertisement is not proof of OnlyFans account-level capacity.

## The 90-minute gate

For 23,897 fans, even one page/fan requires 23,897 page operations, currently 47,794 HTTP requests. Over 90 minutes that is 8.85 HTTP requests/second BEFORE discovery, other lanes, retries, and memory. Two pages/fan need 17.70 HTTP requests/second; five need 44.25. These are conditional requirements, not Maile's measured history depth. Faster software alone does not prove the upstream can supply those rates.

Time onboarding from successful account verification through completed discovery, provider end-of-history for all discovered fan archives, canonical message/offer persistence, and current memory receipts. Import and learning overlap; neither can be omitted from the finish time. Report unavailable/blocked fans explicitly. A partially imported account, older model-memory checkpoint, or sampled fan is not an account-completion result. Purchase and pricing correctness remain tied to verified ledger/offer evidence. Measure media description/content-analysis work separately and include it when required for the declared onboarding outcome; this patch does not make every media file analysed.

Full-source capture and memory accuracy are different acceptance checks. Retain evidence, dates, corrections and manual locks. Run known source-change, subject-attribution, temporal/correction, and refusal regressions. This capacity patch does not certify perfect semantic memory or add missing outbound-context features.

## Stop and rollback

Stop the dedicated importer and learner, drain bounded in-flight requests, and restore the old capacity map before software rollback. Backoff, cursors, messages, purchases and memory receipts are preserved. The old binary checks schema HEAD=0105_earnings_totals, so an image-only rollback against 0106 is not compatible. The downgrade refuses active read leases; after draining, downgrade the migration (transient token count clamps to 10), verify schema 0105_earnings_totals, and restore the recorded compatible worker build. Snapshot current counters and the release manifest before and after. Do not reset archive positions or delete canonical data.
