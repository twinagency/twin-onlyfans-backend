> Historical pre-deployment snapshot. Current deployed status, tests and rollback: [COMPLETION.md](COMPLETION.md).

# Contextual FASTT offers: candidate and release gates

30 October 2026, Australia/Brisbane. This is a code candidate, not a production release.

## Status

| Stage | Result |
| --- | --- |
| Coded | Backend extension and paired dashboard control prepared |
| Tested | Synthetic native turns, durable guards, existing native regressions and focused dashboard type checks |
| Deployed | No |
| Enabled | No; all five production accounts still lack the key, which means Off |
| Provider-send verified for this feature | No; test provider is synthetic |
| Purchase/unlock verified for this feature | No |
| Real rollout scope | Zero creators, zero fans |

The branch is based on attribution fix `6a8c99ec2cb096422f4054205120adaa4a6f1294` (PR #55), so the creator/fan subject fences and dated memory are preserved. This is a stacked review branch, not a claim that its complete repository tree represents production. The backend default is `codex/onlyfans-of-b1`; there is no backend `main` branch. Dashboard base is `3a516c8a60401f8d9ed9b30d3e2666f90d7af821` on `main`. Production has independent overlays and image labels that do not describe its entire source tree. Reconcile those before building a release; never replace production with this branch merely to obtain the feature.

## What changes

* `onlyfans_runtime/contextual_mode.py` and `configuration.py`: validated Off/Shadow/Live field in the existing creator editor, independent from the legacy sales switches. Missing/corrupt saved values fail closed; corrupt values emit a diagnostic. Existing revision, actor, fingerprint, idempotency and immutable audit checks own the update. A field-only save preserves prices, caps, scope, persona and other switches.
* `automations/_sell_signal.py`, `content_prompts.py` and `contextual_offers.py`: a distinct contextual decision and conditional reuse of the existing hook read, MATCH and VERIFY. Cheap gates and a real account-owned shelf match run first. The normal conversation prompt and generated draft are unchanged. Initial opportunities are deliberately limited to concrete shower/bath, gym/workout, beach/swimming/pool, bed/bedroom and cooking/kitchen references. Existing pack language support remains English. Other conversation continues normally.
* `sell_lane.py`, `pack_sender.py` and `ai_chatter.py`: contextual relevance enters its own trigger-aware gate, without manufacturing an ask. Exact content is resolved before any asset claim. Strict contextual matches do not use generic substitutes. Existing FASTT fixed/dynamic pricing, content composition, caption audit, attribution and transport remain in charge. In Live, the one paid card wins the turn; the normal draft is not also sent. No new sender, timer, global ladder or fan activation is added. The initial path chooses chat or a matched paid card; it does not send speculative suggestion-only messages.
* `native_pilot/contextual.py`, `runner.py`, `service.py`, `engine.py`, `postsale.py` and `recovery.py`: durable original-turn and canonical-media identity, frozen content/price/preview/creator revisions, one unresolved contextual-origin slot, and fresh checks at the existing transport boundary. The existing native dispatch attempt/lease/receipt is used. Unknown outcomes remain held. Positive receipt reconciliation links the origin without a new POST; a retained receipt alone never becomes a purchase.
* Same already-offered media is refused regardless of changed price/caption. The ordinary reply references the existing card on a matching ask. Different explicit requested items remain eligible under normal permissions and limits. A contextual origin is excluded only from the explicit-request message-gap calculation; daily, account, monetary and open-offer ceilings remain in force.
* Ignored contextual cards cannot schedule the card nudge, enter discounted replacement scans or execute an already-queued nudge/discount against their unresolved fan. Confirmed post-purchase behavior remains separate and ledger-backed.
* Paired `twin-dashboard` changes use `ConfigurationEditor.tsx`, `lib/onlyfans/configuration.ts`, the existing synthetic fixture and CI. Label: **Contextual offers**. Off / Preview / On map to off / shadow / live. Copy: “Offer relevant available content during AI conversations.” Older API responses remain readable; the new control is disabled until its backend field exists. Deploy compatible dashboard parsers across all consumers before the backend starts returning the additive field.

Shadow resolves and records matching evidence and the candidate action, but never calls the sale planner, creates a catalogue/offer row or send reservation, changes the normal draft, or marks content offered. Its log explicitly says pricing is not planned. Therefore a shadow match is not proof that a priced card would have passed every later send guard.

## Fresh production baseline

Read-only check at **14:41:51 AEST**, host `twin-dashboard-prod`, `i-09a9dd3bd749643a5`, `ap-southeast-2`. Full sanitized observations: [live-baseline.json](live-baseline.json).

Compose: `/etc/twin-onlyfans/media-agency-release-compose.json`.
SHA-256: `b0837e09491a16aaa418ff44f461e8e5bc840fbe8c27bb4d29ed0b4e4d8ac510`.
Schema observed: `0100_manual_free_media`.

| Component | Exact pre-feature image ID |
| --- | --- |
| API, `twin-crm-attribution-20261030:api` | `sha256:49cf1882099727ab3a6447f3df082c24343572ac4f800242f3f26362cd54b472` |
| FASTT sender, `twin-crm-attribution-20261030:sender` | `sha256:deff89ed978f4e59236387abab1c4bde85aed0e649e0fe23f1a10b009ae47bf2` |
| Sync worker, `twin-paid-media-repair-20261029:worker-candidate` | `sha256:eb49d6dac1aafa4167a11625256ebaecfb06a5a4f7681c8e38b3049317a64579` |
| `next-app` and `next-app-of` | `sha256:b3d710711da8566d1eae821d11ee38022f5b6833f68e8d1a9774c23bb0a8793a` |

All five inspected containers were running with zero restarts. No feature image, rollback tag, server-side release record or production schema change was created by this work. These are newly observed baseline IDs, not reused historical rollback assumptions.

Nicky (`of-176774528`) is runtime-disabled, connection-paused, revision 10, `disconnected_by_owner`. It was not reconnected, used for testing or selected for rollout. Katie (`of-210806616`) remains paused/runtime-disabled. No account configuration was repaired. The currently verified/runtime-enabled accounts are Maile (`of-323358988`), Alva (`of-87030857`) and Naomi (`of-99421145`); none is a current contextual canary.

## Purchase-state dependency remains a release blocker

| Connected creator | Last successful recent scan, AEST | Recent cursor, 14:21 → 14:41 | Historical cursor, same interval |
| --- | --- | --- | --- |
| Maile | None recorded | 2400 → 2460 | 2840 → 2900 |
| Alva | None recorded | 2180 → 2240 | 2540 → 2560 |
| Naomi | 29 Oct 17:04:52 | 1960 → 1960 | 780 → 780 |

All three still report `provider_read_deferred`. Earlier observation found nine tokens and recent-message read leases held on these accounts. The deterministic lease test reproduces occupied-lease refusal despite available tokens, verifies that a foreign release cannot unlock it, and that ledger work can acquire only after the owner releases. This is not proof of provider rate limiting, fleet scheduling fairness or a complete contention root cause.

Two cursors advanced slowly; Naomi's were flat. Total remaining backlog is unknown. Cursor movement is not successful recent purchase reconciliation. The existing recent scan can start with a 90-day window; an independent scheduler review must establish a usable recent head without waiting for historical backfill, reproduce fairness under message load, and retain shared budgets/one-owner fencing. No scheduler repair is mixed into this PR.

The new action requires current-binding/current-connection, positively covered recent ledger state with no scan error. Freshness uses the existing 300-second poll interval plus the existing 45-second read lease, measured from the earlier of query cutoff and completion. It is tested to refuse stale, missing, future, held, mismatched and unproven state. The polling constants are documented assumptions, not evidence that production currently meets them. Ordinary conversation is the fallback. This feature must not be enabled while the dependency remains unhealthy.

## Validation and limits

See [validation.json](validation.json) for exact commands, results and unavailable checks.

* Focused behavior tests cover mode validation/audit preservation, source evidence, active native authority, account/fan holds, stale purchase state, content/price changes before send, concurrent workers, replay/unknown outcomes, ignored/expired offers, same/owned versus different media, positive recovery, and non-destructive migration rollback.
* Full synthetic FASTT turns run its real conversation, resolver, pricing, plan, caption and delivery code with model/provider boundaries mocked and network denied: Off has one ordinary reply and no extra model call; Shadow has the same ordinary reply and zero offers; Live has one matched paid card and no duplicate normal reply. Both dynamic pricing and a saved 700-cent fixed price are exercised.
* Eighteen existing native regression modules pass, including sender/receipt/unknown protection, service/control behavior, on-request and post-buy selling, follow-ups, 2,000-per-fan allowance checks, and all six attribution tests.
* Dashboard configuration contract and focused editor dependency type checks pass. Full application build, browser/operator walkthrough and all-brand deployment compatibility are not established by these focused checks.
* PostgreSQL configuration/permission, manual free/paid-media, ledger/load and complete schema rehearsal tests were not run. A disposable local PostgreSQL attempt could not create/use a non-root mapped UID in this runtime. The existing SQLite `test_native_pilot_runtime.py` fails on PostgreSQL-only JSONB DDL on both the untouched baseline and candidate; it is not reported as a passing regression. CI/staging must supply PostgreSQL before rollout.
* No real fan messages, purchases, model spending or provider sends were initiated. No conversion uplift, live manual-media success or current purchase/unlock cycle is claimed.

## Deployment procedure and exact stop conditions

1. Resolve the canonical release source through review. Preserve attribution PR #55 and every current FASTT/media/manual/memory overlay. The default/backend-main mismatch and historical pinned workflow artifacts must be reconciled before using a main-only release policy. Do not merge or deploy an older tree. Current PRs are review candidates only.
2. In the release pipeline, re-read every image ID, compose hash, source hash, mount, environment reference, connection/configuration revision and schema version. Abort on drift and capture a new baseline. Export the current compose, executable images and protected schema/config backup into a **new** per-run release record, e.g. `/var/lib/twin-release/contextual-offers-20261030/<run-id>`. This path is a required future artifact, not an existing release record. Keep secrets out of Git and preserve mounts and signing keys.
3. Pass the missing PostgreSQL integration suites and a populated upgrade/downgrade rehearsal. Repair and independently review recent-transaction scheduling first. Demonstrate successful recent scans under message load without unsafe lease overlap, reply starvation, changed provider throttles or full-backfill dependence. Verify manual free/paid sending and takeover using an explicitly authorized synthetic fixture/account, with no test purchase or unsolicited real-fan traffic.
4. Build immutable paired artifacts from the reconciled release, retaining exact parent IDs. Deploy compatible configuration parsers to every dashboard consumer. Because schema HEAD becomes `0101_contextual_offers`, coordinate every process that requires an exact schema head, including API, sender and worker. Stop those processes through the pipeline for the migration; do not rewrite any fan activation state. Apply `PYTHONPATH=service python -m db.schema upgrade --url-env ONLYFANS_MIGRATION_URL` using the established explicit database selector. Verify schema and source/health before resuming. Initial stored contextual mode remains missing/Off. No price, cap, allowance, account connection or fan scope may be changed by the release.
5. Only after these gates, choose one currently connected, already AI-enabled and operational creator in the authorized scope. Exclude Nicky; do not incidentally repair Katie or Alva. Record the exact existing creator/fan scope before changing only its contextual mode to Shadow through the existing audited API/dashboard. Observe real positive, negative, ignored and no-match cases. Insufficient traffic means unverified, not passed.
6. A bounded Live canary requires reviewed shadow evidence, fresh purchase state, available unchanged limits, and an explicitly recorded creator/cohort/window. No canary was selected here. Observe the first naturally occurring sends; independently match provider receipt, original turn, canonical media, caption and price. Record purchase, unlock and ledger matching separately only if they occur. Do not initiate purchases or customer test messages to manufacture evidence.
7. Immediately turn Off for duplicate/wrong creator/media/price, takeover violation, ignored-offer chase or unintended activation. No expansion until missing acceptance evidence is resolved. Compare opportunity/decision/send/error/purchase/decline/suppression counts; a few sends establish no conversion uplift.

## Kill switch, migration and rollback

Kill switch: save only `fasttContextualSalesMode: "off"` in the existing creator configuration flow, with its current revision/fingerprint and idempotency key. The saved key is `fastt_contextual_sales_mode`. Existing on-request permissions remain unchanged. Native transport re-reads the mode, connection/configuration revision, holds and snapshot immediately before posting; a previously inferred contextual action loses authority when Off.

Migration `0101_contextual_offers` adds only `of_contextual_offer`. Its unique original-turn identity and partial unique unresolved-fan slot preserve decisions across workers and restarts. It changes no existing row or allowance. Downgrade to `0100_manual_free_media` deliberately retains this evidence table; a later upgrade reuses it. Unknown/reserved rows are never cleared just because a service restarted, mode changed, offer expired or fan chatted.

Rollback through the release pipeline, using the actual per-run preflight record:

1. Set contextual mode Off for every canary creator and stop new contextual admission. Stop/drain the affected sender processes; reconcile all dispatching/unknown attempts using positive provider evidence before any retry. Do not infer non-delivery, delete reservations or replay inputs.
2. With incompatible schema consumers stopped, use the established explicit selector to run `PYTHONPATH=service python -m alembic -c service/db/migrations/alembic.ini downgrade 0100_manual_free_media` **only if the feature is the sole schema advance**, with the established `CHATTERLY_DB_URL` environment selector injected by the pipeline. Retain the contextual table, offers and receipts. If newer schema work exists, require a compatible forward rollback instead.
3. Restore API, sender and worker to the exact IDs captured for that run. If the current baseline is still the one recorded here, those IDs are respectively `49cf1882…`, `deff89ed…` and `eb49d6da…`, with the complete immutable IDs above. Restore the protected compose/environment/mount references from that same run. Never use unrelated historical rollback tags.
4. Restore dashboard `b3d71071…` only after the backend contract is compatible; both Twin containers had the same complete digest above. Preserve all real messages, purchase records, connection/activation state and existing on-request/post-buy configuration. Check health, source hashes and existing manual/chat behavior. Record rollback revision and remaining reconciliation holds separately.

No production rollback was executed because this candidate was never deployed.
