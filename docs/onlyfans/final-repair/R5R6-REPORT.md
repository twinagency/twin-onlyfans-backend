# R5 + R6 report (LOCAL ONLY)

Branch `claude/of-final-r5r6-20261101`, worktree `twin-onlyfans-backend-wt-final-r5r6`, base `6aabad9`.
Nothing was pushed, deployed, activated, emailed, or sent. No production database, SSM or provider call. Telegram/FLIRT
code paths are untouched; every OnlyFans behaviour change sits behind the existing per-creator contextual mode
(off/shadow/live) and with the mode `off` nothing is recorded and nothing differs.

Labels: DONE = built and covered by a test in this branch. PARTIAL = built for the named path only. NOT DONE = not built.

## R5 active selling

| Plan item | Status | What exists |
|---|---|---|
| 1. One action decision per eligible turn | PARTIAL | `automations/sales_action.py` `decide()` is a pure typed result (answer, clarify, direct_offer, proactive_offer, invite, develop, unavailable_alternative, support, handover_stop). It is called and recorded in two real places: the contextual path (`contextual_offers.plan_contextual`) and the direct-ask path in `ai_chatter` (right after `lane.plan`). The support/handover/hold inputs exist in `ActionInputs` and are tested, but the engine does not yet feed them from the R1 policy for the decision row; those holds still stop sends where they always did, in their own code. |
| 2. Direct request = actual offer in the same turn | PARTIAL | Behaviour unchanged (the existing ask lane sends the card and replaces the draft). Decision row records `direct_offer` or the refusal reason. No new answer-plus-card bundling was added. F02 interview/earning loops were not touched. |
| 3. Proactive offer from fan-side evidence | DONE (on existing PR #68 path) | Evidence ids come from the hook's fan messages. Controls now weight the outcome: current interest -> card (standard/active) or an invitation (lower initiative, no pacing pressure). Older/background interest -> develop/invite. |
| 4. Revalidate content revisions for edits/retractions | NOT DONE | Existing newest-message check kept as is. |
| 5. After purchase / rejection / unpaid card | PARTIAL | Decision-level tests: payment arriving never triggers a card, a fresh request is served at once, a recent refusal or an ignored invitation of the same item is not chased, a different item is still eligible, an identical unresolved card blocks. Not exercised through the full engine. |
| 6. Remove/narrow stale vetoes | NOT DONE | Not audited. Existing money/cap brakes untouched. |
| 7. Observability | DONE (two paths) | Table `of_sales_decision` (one row per turn+stage: mode, action, approach, outcome, reason, settings in force, evidence, grouped turns, raw bubbles, invitation id). Excluded and blocked turns are rows. `automations/sales_decisions.py report()` gives denominators incl. excluded/blocked, first vs repeat pacing separately, invitations by state. |
| 8. Cadence benchmark | DONE | `service/tools/first_offer_benchmark.py`: read-only SELECT, anonymised creator numbering, first vs repeat, grouped turns and raw bubbles separate, coverage incl. conversations that never reach a card. Not run on real data (none available). |
| 9. PR #76 multi-message evidence through real authority + engine | NOT DONE | Not attempted. |
| 10. Slang delivery-promise guard bound to order state | PARTIAL | `quote.wording_gate` / `boundary_hold` (see R6). `pack_claim._DELIVERY_PROMISE` unchanged. |

Clarification (Ronan, 1 Nov evening):
- Three approaches: offer / invite / develop are the `approach` values; no rotation exists.
- Pacing references: `pacing()` computes position (before/within/beyond range `[ref-1, 2*ref]`) and a 0-2 pressure that weights choice. A count alone never produces a card (test sweeps 0..39 turns x 3 initiatives x 2 evidence levels).
- Controls (`onlyfans_runtime/commercial_controls.py`): `fasttCommercialInitiative` lower|standard|active, `fasttFirstOfferReference` 1-12, `fasttRepeatOfferReference` 3-6 (labelled experimental in the view). Stored in the existing saved sales JSON, validated, patchable through the existing `configuration.validate_patch` interface, visible in the config view. Invalid saved values fall back per setting. `ACTIVE_PILOT_PROFILE` is a prepared constant that no code applies.
- Invitations (`automations/sales_invitation.py`, table `of_sales_invitation`): stored per account/fan/item/fan-side source message; one open at a time (a newer one supersedes); a bare yes or thumbs-up accepts it only if it reached the fan, no other fan message intervened, and it is unambiguous; acceptance is idempotent for the accepting turn; revalidated against product readiness before the card is planned; acceptance presents the offer, never charges. Declined/ignored invitations are remembered.
- Repeat offers: separate setting and separate reporting section; no send from silence/time/count.
- Reporting: invitations, decisions that chose a card, delivered cards (existing `content_offers`) and purchases stay separate counts.
- Off/shadow/live: off records nothing; shadow records the decision with mode `shadow` and stores/sends nothing; live acts.

Behaviour change to be aware of: with the default `standard` initiative, current-interest flow is identical to before. `lower` is opt-in. Two new protections apply in every mode other than off: a recent genuine decline of the same media suppresses a new contextual card for it (`recent_refusal`).

## R6 canonical quote, payment, fulfilment

| Plan item | Status | What exists |
|---|---|---|
| 1. Quote authority on the existing record | DONE | The existing `content_offers` row carries the quote: new nullable `quote_json`, `currency`, `obligation_id`, `lifecycle`, `quote_version` (no new ledger). `onlyfans_runtime/quote.py` `price()` is the only function that computes an amount; `format_price`/`text_consistent` render and check text amounts. The native receipt projection (`media_delivery.project`) fills the quote on the offer it creates (`quote.attach_to_offer`). |
| 2. Pricing policy | PARTIAL | `price()` applies `upsell.may_discount`/`discount_price` only where the creator saved `fastt_discount_resend` true (15% first cut, 20% total, third refused), honours a stated budget in its scope (per_item/lifetime; scope required), never charges above it without the fan's agreement (`over_budget` + eligible cheaper items), explains floor/provider-minimum, accepted quotes are immutable, no silent raise. Tests reproduce the p285-286 ($4.11 vs $3), p307-308 ($4.20 vs $3) and p236-237 ($79.99 vs $80) shapes. NOT wired as the live caller for the existing native dynamic quote (`native_sales.choice`/`_quote_item`) or the upseller; those still compute their own amounts. |
| 3. Payment routes / obligation id | PARTIAL | `obligation_id(route, ...)` is deterministic over tip/locked_card/custom_service. Native prepare (`text_delivery`) refuses a non-manual send when an unresolved request for the same obligation exists ("payment already requested for this obligation"); a partial unique index backs it. Tip and custom-service routes are modelled but no sender uses them. |
| 4. Delivery lifecycle | PARTIAL | `lifecycle` column plus forward-only state machine (quoted, awaiting_payment, dispatch_intent, provider_accepted, reconciled, purchased, fulfilled, escalated/cancelled), `may_dispatch`, `reconcile_unknown`, stable `idempotency_key`. The receipt projection sets `reconciled`. The existing `OfTextIntent`/attempt machinery still owns dispatch; the intermediate states are not yet written by it. |
| 5. Wording gate | PARTIAL | `wording_gate` (state-aware helper) and `boundary_hold` wired into the engine's text boundary: no fulfilment deadline and no completed-refund statement is ever sent by the automatic chatter (held with `deadline-unbacked` / `refund-claim-unbacked`). Sent-claims and promises were already held by `claims_media_sent`; `wording_gate` is the state-aware version and is not yet called by a sender. |
| 6. Ownership durability | NOT DONE | Not changed. |
| 7. Complaints | PARTIAL | `dispute_evidence()` returns ordered assets/versions for the human. Complaint selling suppression already exists (R1 policy); decision layer maps `support:complaint` to SUPPORT. No refund tooling. |
| 8. Customs (F27) | PARTIAL | `open_custom_case` creates an R1 `of_operator_case` (category `custom_request`, scope `offer`) only with verified availability, agreed scope, price, permitted route, a named owner and a deadline; otherwise returns every missing requirement and creates nothing. Not connected to R3's `operator_case_port` (no sink is registered anywhere in production code; R3 cases still go nowhere). |

## Schema

Migration `0107_active_selling_quote` (`db/migrations/versions/20261101_0400_active_selling_quote.py`), chained after `0106_read_lane_fairness`; idempotent via `sa.inspect` (columns, index, `checkfirst` tables). Adds tables `of_sales_decision`, `of_sales_invitation` and five nullable columns + partial unique index on `content_offers`. Downgrade is a no-op by design (sale evidence). `db/schema.py` HEAD/REVISION_RANK updated; HEAD asserts updated in four tests.

## Tests (disposable PG container `ofpg-r5r6`, port 55440, removed afterwards)

Command pattern, from `service/`: `PYTHONPATH=. python -m pytest -q -p no:cacheprovider -W ignore tests/<file>`; files pytest collects nothing from were run as scripts. PG env: `ONLYFANS_TEST_PG_URL` and `NATIVE_PILOT_TEST_PG_URL` -> the container.

New, all pass:
- `tests/test_active_selling.py` 42 passed (decision, controls, pacing, invitations, decision log/report)
- `tests/test_active_selling_turn.py` 1 passed (script, real NativeEngine turn, 6 scenarios: standard card, lower initiative invitation, "yes" accepts that invitation, bare yes sells nothing, mode off writes nothing, shadow records only)
- `tests/test_quote_lifecycle.py` 40 passed
- `tests/test_first_offer_benchmark.py` 3 passed
- Regressions-first-on-baseline logs were NOT produced (the new modules do not exist on the base, so those tests cannot run there).

Existing suites touched or adjacent, pass: contextual_evidence 70, contextual_evidence_scope 17, contextual_offers 52, contextual_item_scope_migration, contextual_native_turn (script), contextual_release_pg, product_readiness 9, substitution_agreement 6, native_pilot_* (identity 25, model 15, receipt_recovery 17, recovery 9 and about 14 script suites), native_reply_format/pacing, r3_* and r3r7_inbound_intent, onlyfans_text_delivery_pg 42 (includes the new duplicate-obligation guard), onlyfans_migration_pg (runs the chain through 0107), throughput_migration_pg, ledger_exclusion_pg 9, read_lane_fairness_pg, ledger_read_fairness_pg 8, reply_configuration_pg 15, fixed_price_pg 11, launch_*_pg, native_pilot_attribution_pg, daily_allowance_pg, fastt_read_priority_pg, concurrent_admission_units 7, creator_opening_prices 24.

Failures that are NOT from this branch (same result on `final-integ` or explained by the base):
- `native_pilot_resolver_seam`, `native_pilot_postsale_engine`: stale fixtures at base 6aabad9 (missing content_hash); the later integration commits 4cda8bb/1f67b9f fix them; with those two test files copied in, both pass on this branch. Not cherry-picked here.
- Identical failure on the integration tree: `native_pilot_context`, `native_pilot_reporting` (psycopg2 not async, env), `native_pilot_expiry`, `native_pilot_runner`, `native_pilot_postsale` (9 errors), `native_pilot_load_pg`, `onlyfans_native_planning_pg` (1: stale native planning claim), `r3r7_native_planning_pg` (6), `conversation_holds_pg` (1 of 39: two_workers), `onlyfans_native_upgrade_pg` (3), `onlyfans_release_package_pg` (1+3 errors, needs pg client binaries).
- `native_pilot_storage_pg`: fails because the test pins LIVE_REVISION `0103`; stale at the base already.
- `native_pilot_service_pg`: times out waiting for a first reply, same "native-turn-failed" signature as load_pg; baseline run NOT done.
- Not run: `test_native_pilot_*` PG variants other than those listed; dashboard-side tests; full repo suite.

## F-rows
Contributes to, but does not fully close, anything: F01/F02/F06/F11/F15 (decision layer + invitations exist; real incident fixtures p62..p299 were NOT replayed), F03/F09 (duplicate-request guard, wording hold; senders not unified), F27 (case requirements helper only). None marked closed.

## Open risks and decisions needed
1. The quote is not yet the single price writer: the native dynamic quote (`native_sales.choice`) and upseller still compute amounts. Decision: route them through `quote.price()` (touches live pricing).
2. Lifecycle states between awaiting_payment and provider_accepted are not written by the existing intent/attempt code; only `reconciled` is set at receipt projection.
3. R3 `operator_case_port` still has no registered sink in production code; `open_custom_case` is not called by the engine.
4. `lower` initiative invitation uses the hook's bridge line plus "want me to show you?"; wording quality untested with a real model.
5. A recent decline of the same media now suppresses a contextual card (all non-off modes); confirm that is wanted.
6. Migration 0107 adds a partial unique index on a shared table (`content_offers`); review lock time on the production row count before any release.
7. The decision/invitation hooks add a few small DB reads per eligible turn when mode is shadow/live; not load-tested (R8).
