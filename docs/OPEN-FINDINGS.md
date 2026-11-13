# Open-findings triage — W1+W2 (2026-11-12)

Review-only windows; nothing fixed by us. Team owns fixes; this file ranks them. IDs/counts only (public repo).

- Source: `.scratch/rwl-W1/STATE.md`, `.scratch/rwl-W2/STATE.md` (Threads, Summary, Orphan), `.scratch/rwl-programme/STATE.md` (Live-risk, OQ-1..9, Severity policy, Disputes), `git show rwl/W<n>-<slug>:docs/review/W<n>-<slug>/FINDINGS.md` for 16 review lanes, live results `.scratch/rwl-W{1,2}/live-verify/RESULTS.md`.
- Pins: W1 = 659ea29..e8e90954, W2 = e8e90954..a2f1dda6. Live trial commit = 659ea29.
- Reach ref (W4 triage): `659ea29` lines renamed to the running build `8d0001f9` (jaka/takeover-20261111; contains 659ea29 + e8e90954, not a2f1dda6; m W4 live-verify). Values still measured at 659ea29. Each `n` whose cited file changed in 659ea29..e8e90954 (`git diff --stat`, local refs, no fetch) → `reach: re-check (file changed)`; code not re-reviewed.
- Reach pass (2026-11-12): every G1/G2/G4/G5/G6 id marked `n` / `re-check` now has `running 8d0001f9: y/n` (m: `git show 8d0001f9:` / `git grep`). service/ at 8d0001f9 = e8e90954 + browserbase_login.py, connection_api.py, session_bootstrap.py only (m `git diff --stat`) → W1 cites hold at the same lines; W2-added files (history_ownership, ownership_gate, catalogue_select, history_import, message_facts, scope_contract, commercial_lifecycle, price_intent, catalogue_terms) absent. 74 ids: y 33, n 41, ? 0.
- Live stack (measured, both live-verify lanes, 0 OF calls): DB at `0133`; pin head `0136` → every finding on 0134–0136 objects is latent on the running stack. Sending disabled: 0 delivery policies, runtime off, 0 scope / intent / delivery / grant rows, 0 automations, tip_request off. 0 vault / catalogue / evidence / model-call rows → most sellability + ownership checks vacuous (OQ-6).
- Rule: severity = harm if reached (written rule); Reach = live|latent + the one guard keeping it latent + m/i/g (programme Severity policy). Latent blocker gates lifting its guard, not a merge.
- Dedupe: by file:line across lanes/windows; W2 "W1 carry-over" rows are restatements (not recounted). Cross-lane dups noted `=`.
- Picks: bug/data-risk first (send twice / wrong fan, cross-tenant, money/ledger/ownership, data loss, race, 5xx); ≤6 groups. Everything else one word.

## Counts

- raw: 404 lane-local ids (W1 222 = L1 35, L2 11, L3 36, L4 26, L5 26, L6 16, L7 29, L8 43; W2 182 = db 19, scope 22, sale 36, runtime 25, fans-api 27, tests-commercial 21, tests-history 21, claims-sim 11). Measured by grep of FINDINGS ids; W2 sale focus labels F1–F8 excluded.
- picked: 92 ids in 6 groups. rest: 312 (lane-local ids; ` = ` dups inside groups still counted per id).
- running 8d0001f9 (m, reach pass): re-checked ids only: G1 y 2 / n 19 (W2OR-03/-05 halves); G2 y 5 / n 6; G4 y 9 / n 2; G5 y 14 / n 8 (incl. W4MT-1); G6 y 3 / n 6. Prior y kept: DOC-10/CS-02, F01, N1, W1-04, F6. G3 n. Rest: S1 (R11/R13) y m, W2SC-14 y m.
- red tests on lane branches: W1 25, W2 45 (THERMO). None run against the live stack.

| verdict | ids |
|---|---|
| pick (G1–G6) | 92 |
| latent | 66 |
| test-only | 71 |
| structure | 68 |
| YAGNI | 52 |
| doc | 39 |
| dropped | 7 |
| dup | 4 |
| owner | 4 |
| fixed | 1 |
| **total** | 404 |

## Groups

### G1 · Ownership gap → re-offer of already-bought media (money, wrong card to fan)
- ids: DOC-10 = W2S-02 = CS-02 (carry-over, ownership.py:179-185, history_ownership.py:164-168); W2S-03, W2S-04, W2S-06, W2S-07, W2S-11, W2S-12, W2S-16, CS-06; W2OR-03, W2OR-05 (manual-vault / Moxy cards skip `ownership_gate`), W2OR-21; W2FA-01, W2FA-02 (cond), W2FA-03, W2FA-04, W2FA-07 (cond), W2FA-08, W2FA-11, W2FA-15, W2FA-17; W2SC-06 (partial coverage reported complete).
- what: missing / malformed / non-message (post, stream) / dismissed purchase evidence reads as "not owned"; history derive turns unknown → clear (count-vs-count, fact-less unlock, `ours` not fan-scoped, conflicting tx ignored); import keyed by account only.
- severity: blocker (REVIEW #9, #11). Reach: latent — guard on live stack = sending disabled + 0 evidence rows (m). In code @659ea29: commercial money-evidence hold `pilot_scope.delivery_block` holds evidenced fans (m); residual re-offer (non-message PPV, history gaps, dismissed rows, malformed JSON) i. W2S-03/04 + W2FA-* need 0136 tables (absent live, m).
- running 8d0001f9 (m): DOC-10/CS-02 y (prior, narrowed). **y**: W2OR-03 manual half (text_delivery.py:218-229, :635-641 → media_delivery.py:100-103 `manual_held`, no unknown-ownership check; gate half absent); W2OR-05 Moxy half (text_delivery.py:230-232, :642-645; Moxy never calls `catalog_terms` / `assert_unowned`; terms half absent). **n** (history_ownership / ownership_gate / catalogue_select / history_import / message_facts / history models absent): W2S-03, W2S-04, W2S-06, W2S-07 (`_commercial_purchases` absent), W2S-11 (engine.py:184 `pending,held` predicate alone present, no split), W2S-12, W2S-16, CS-06, W2OR-21, W2FA-01, W2FA-02, W2FA-03 (events.py:561 `not_a_message_payment` ignore present; coverage half absent → harm = DOC-10 path), W2FA-04, W2FA-07, W2FA-08, W2FA-11, W2FA-15 (events.py:782-810 no binding check present; account-keyed import tables absent), W2FA-17, W2SC-06 (no `history_import.eligibility` in preparation.py).
- fix: no evidence / no import / malformed JSON / non-message purchase / dismissed money row ⇒ ownership `unknown` ⇒ refuse paid card; scope history lookups by fan + binding.
- red: `rwl/W2-conversation-engine-sale:service/tests/test_w2_conversation_engine_sale_pg.py` (w2s02, w2s03, w2s04b, w2s07, w2s11); `rwl/W2-fans-api-adapter:service/tests/test_w2_fans_api_adapter_pg.py` (w2fa_01 ×2, w2fa_03). DOC-10 malformed-JSON half: idea only.
- OQ: OQ-8 (import freeze → W2FA-17); W2FA-02/07 conditional defaults "no" (lane, not numbered).

### G2 · Emergency stop refusable; dashboard activate widens commercial scope (send after stop)
- ids: W2SC-02, W2SC-11, W2SC-22 = W2OR-04, W2OR-18, W2OR-19, W2OR-20, W2OR-08, W2OR-09, W2OR-10; F03 (W1 L4, connections.py:332-344).
- what: stop/pause re-validates scope + limits inside `_configure_in_session(scope_stop=True)` → `Refused` rolls back runtime-off + queue cancel; activate sets native + media + 10000 units + runtime on with no scope check, admission admits any fan; then stop is refused.
- severity: blocker (inv. 4 "stop never refusable"). Reach: latent — guard = no commercial scope can exist live (DB 0133 < 0134, 0 scope rows; m). After 0134 the only guard is nobody pressing dashboard activate (m by reading).
- running 8d0001f9 (m): **y**: W2SC-22 = W2OR-04 + F03 (connections.py:332-344 activate → native, media, limit, runtime on, no scope check); W2OR-19 (activate flips to native → fansapi-keyed guards text_delivery.py:582-594 stop applying); W2SC-02 pilot-stop variant only (pilot_scope.py:703-715 text-only stop → text_delivery.py:279-284 authorize + limit check → `Refused` rolls back the stop; after activate limit 10000 > scope). **n** (commercial_lifecycle / scope_contract / `scope_stop` absent; emergency stop = launch_operations.py:144 `stop_runtime_in_session` only): W2SC-02 emergency-stop core, W2SC-11, W2OR-18, W2OR-20, W2OR-08, W2OR-09, W2OR-10.
- fix: one validation-free `stop_scope` (runtime off, queue cancelled, policy off, scope stopped, no config read); activate calls `assert_enable_allowed`, refuses on commercial/preparation scope.
- red: `rwl/W2-conversation-engine-scope:service/tests/test_w2_scope_commercial_route_pg.py` (emergency_stop ×2); `rwl/W2-onlyfans-runtime:service/tests/test_w2_onlyfans_runtime_pg.py` (w2or04, w2or18).
- OQ: OQ-7 (non-audience sends on active scope; default hold).

### G3 · Terms-only commercial route mints standing grant without promotion (wrong fan / unaudited sale)
- ids: W2SC-01, W2SC-03, W2SC-04, W2S-01, W2S-10, W2S-27, W2S-30, CS-03, W2DB-01, W2DB-04 (terms row can price another account's item/binding, inv. 8), W2DB-05, W2DB-08.
- what: any approved `of_catalogue_terms` row makes an unpromoted account "commercial"; `prepare_commercial_offer` mints from caller `coverage_ref`/`valid_until` without #4 holds or audience; damaged scope row = "no pilot" opens holds; promotion FK accepts any launch command; lineage NULL passes.
- severity: blocker. Reach: latent — guard = `of_catalogue_terms` absent live (0135 not applied, m) + `prepare_commercial_offer` has no non-test caller (m).
- 8d0001f9 (was 659ea29): n (m: whole route added in W2; commerce.py, pilot_scope.py, scope_contract.py unchanged 659ea29..e8e90954, m).
- fix: one commercial predicate = validated, promoted, active scope row with explicit audience; terms alone never commercial; grant coverage from validated config + `prepare_pilot_offer` hold block; FK terms → same-account item + binding.
- red: `rwl/W2-conversation-engine-scope:service/tests/test_w2_scope_commercial_route_pg.py` (terms_alone, never_mints, damaged ×2); `rwl/W2-conversation-engine-sale:service/tests/test_w2_conversation_engine_sale_pg.py` (w2s01a ×2, w2s01b); `rwl/W2-db-migrations:service/tests/test_w2_db_migrations_pg.py` (w2db01, w2db04 ×2, w2db05).
- OQ: OQ-7.

### G4 · Unsellable media offered (not-ready / error / DRM / vanished / uncertain)
- ids: F01 (W1 L4), N1 (W1 L4 R2), L3-01, L3-02, L3-03, CVA-01, T01, T12, DOC-01, DOC-02, DOC-13, W2S-05, TC-07.
- what: `catalog_terms` checks only `removed_at` + kind; sync un-removes retained not-ready rows; DRM skip deleted; vanished never retired; uncertain items keep earlier category membership; new W2 shortlist/price path adds no DRM/vanished gate; tests pin the open gap as correct (T01).
- severity: blocker (CVA-01, DOC-02 conditional on OQ-4; default per-asset → blocker). Reach: latent — guard live = sending disabled + 0 vault/catalogue rows (m); in code: preparation-scope hold @e8e (m), lifted for an active commercial scope @a2f (i).
- running 8d0001f9 (m): **y** (cited blobs identical to e8e90954, same lines): F01 native + FansAPI case (vault_management.py:855-856), N1, L3-01, L3-02, L3-03, CVA-01, T01, T12, DOC-01, DOC-02, DOC-13. **n** (W2 paths absent: catalogue_select.py, `prepare_commercial_offer`, commercial_terms_fixture.py): W2S-05 (underlying removed_at-only gate = F01 y), TC-07.
- fix: one per-asset readiness gate (ready, canView, !hasError, in window, classification certain; DRM sellable per OQ-3) inside `catalog_terms` / offer prepare for every paid path.
- red: `rwl/W1-fans-api-adapter:service/tests/test_w1_fans_api_adapter_pg.py` (l3_01_02 ×4, l3_01b, l3_03); `rwl/W2-conversation-engine-sale:service/tests/test_w2_conversation_engine_sale_drm_pg.py`; `rwl/W1-commands-vault-ai:service/tests/test_w1_commands_vault_ai_classification.py` (CVA-01); `jaka/rules-tests-20261111:service/tests/test_rule_retained_vault_unsellable_pg.py` (X01).
- OQ: OQ-1, OQ-2, OQ-3 = "DRM sellable (owner, 2026-11-13)" — DRM is not an unusable gate; sent by vault media id (#116 → 85e0f487), OQ-4, OQ-6.

### G5 · Paid-call ledger: unknown model call re-called; paid FansAPI reads re-bought
- ids: W1-04, F6 (both pre-existing), W1-01, W1-02, W1-05 = F2, W1-06, W1-32, F1, F5, L3-05, L3-08 ≈ W2FA-06, L3-09, W2SC-05, W2SC-07, W2SC-08, W2FA-05, W2FA-13, W2FA-14, W2FA-19, DOC-07, DOC-17.
- what: runtime retries a ledger `unknown` row (same-pass fallback / replay) → second possibly-charged call; Retry-After `'²'` turns refusal into held `unknown` (no human); local/unclassified preparation failures non-terminal → identity GET re-bought every pass; Retry-After clamped to 3600 / per-fan only → paid 429 storm; caption path outside ledger.
- severity: blocker (W1-01) / major (rest). Reach: latent — W1-04 guard = scheduler `uncertain` filter + fallback off by default (i; webhook redelivery path bypasses filter, open); live 0 model-call rows (m); paid reads: preparation unconfigured live (0 scope rows, m), bounded by scope budget.
- running 8d0001f9 (m): W1-04, F6 y (prior). **y** (cited blobs identical to e8e90954, same lines; R0 fixes not in build): W1-01, W1-02, W1-05 = F2, W1-06, W1-32, F1, F5, L3-05, L3-08, L3-09, DOC-07, DOC-17. **n** (history_import.py / `SourceBudgetExhausted` absent): W2SC-05, W2SC-07, W2SC-08 (W1-17 base y), W2FA-05, W2FA-06 (vault clamp = L3-08 y), W2FA-13, W2FA-14, W2FA-19.
- fix: `unknown` row ⇒ hold, never re-call; refusal never → `unknown`; local/unclassified failures terminal or counted; honour provider Retry-After per account, no 3600 clamp.
- red: `rwl/W1-model-transport:service/tests/test_w1_model_transport_pg.py` (f1, f2, f5); `rwl/W2-conversation-engine-scope:service/tests/test_w2_scope_history_pg.py` (W2SC-05, W2SC-06); `rwl/W2-fans-api-adapter:service/tests/test_w2_fans_api_adapter_pg.py` (w2fa_05, w2fa_06). W1-04 / W1-01: none (needs PG; lane skipped).
- OQ: none numbered (W1-13 402 handover, unnumbered).

### G6 · Wrong price frozen on a card (money)
- ids: F02 = CVA-11 → W2OR-01 (worse), W2OR-02, W2OR-11, W2OR-12, W2S-08, W2S-09, CVA-08.
- what: `approvedTotalCents` accepted on a single (preview counted as bundle) and frozen as `agency_category` price; category price change never reaches approved singles; keyword `asks_cheaper` grants discounts on non-asks; discount grant replaced by standard successor at higher price; price authz tenant-wide with self-asserted actor.
- severity: major. Reach: latent — guard live = drafts created disabled + 0 vault rows + sending disabled (m); W2OR-01/02 need 0135 (absent, m).
- running 8d0001f9 (m): **y** (blobs identical to e8e90954): F02 = CVA-11 (vault_management.py:485-491), CVA-08 (vault_editing.py:120, vault_management.py:473). **n** (catalogue_terms.py / price_intent.py / `terms_successor` absent): W2OR-01 (F02 base y), W2OR-02, W2OR-11, W2OR-12, W2S-08, W2S-09.
- fix: `approvedTotalCents` only with >1 paid asset; one category-price check shared by drafts + `approve_terms`; `catalog_terms` compares `category_price_revision`; discount variant sticky per grant.
- red: `rwl/W1-onlyfans-runtime:service/tests/test_w1_onlyfans_runtime_category_single_price.py`; `rwl/W2-onlyfans-runtime:service/tests/test_w2_onlyfans_runtime_pg.py` (w2or01 ×2, w2or02); `rwl/W2-conversation-engine-sale:service/tests/test_w2_conversation_engine_sale_price_intent.py` (W2S-08).
- OQ: OQ-1 (operator-priced legacy singles stay sellable).

## Rest

| ids | lane | verdict | note |
|---|---|---|---|
| S1 (R11 ×6 / R13 ×7 reply validator) | W1 L1 | latent | 659ea29 y m; red `jaka/rules-tests-20261111:service/tests/test_rule_reply_validator_denylist.py`; sending off; team |
| W1-03, W1-07, W1-08, W1-09, W1-11, W1-14, W1-15, W1-16, W1-17, W1-18 | W1 L1 | latent | preparation unconfigured live (m) |
| W1-10, W1-20, W1-21, W1-22, W1-23, W1-24, W1-25, W1-26, W1-27, W1-28, W1-29, W1-30 | W1 L1 | structure | W1-24 blocker (dup helpers) |
| W1-31, W1-33 | W1 L1 | dup | merged into W1-24 / W1-21 |
| W1-12, W1-13 | W1 L1 | owner | unnumbered (age bound; 402) |
| W1-19 | W1 L1 | dropped | R2 |
| S2 (R04) | W1 L1 | test-only | test can't localise seam |
| F3 | W1 L2 | latent | blocker: key-shaped / fan text in immutable rows; 0 model-call rows m; red f3 ×4 |
| F4 | W1 L2 | test-only | |
| F9, F10, F12, F16 | W1 L2 | YAGNI | |
| F11 | W1 L2 | dup | = W1-20 |
| DB-01 | W1 L6 | latent | harm absent: live 0133, 0129b cols present (m); DOC-04 same |
| DB-02, DB-03, DB-05, DB-06, DB-17, DB-18 | W1 L6 | latent | |
| DB-04 | W1 L6 | fixed | a2f filters via claim (m) |
| DB-08 | W1 L6 | doc | |
| DB-10, DB-11, DB-12, DB-13, DB-14, DB-15, DB-16 | W1 L6 | structure | |
| F04, F05, F11 | W1 L4 | latent | 5xx; needs enabled category product (0 m) |
| F08, S3 | W1 L4 | latent | native fence hole; binding refusal swallowed |
| F06, F07, F12, F13, F14, F15 | W1 L4 | YAGNI | R2 downgraded |
| F09 | W1 L4 | owner | OQ-2 |
| F10 | W1 L4 | YAGNI | wrong vs design (R2) |
| S1, S2, S4, S5, S6, S7, S8, S9, S10 | W1 L4 | structure | |
| CVA-02, CVA-03, CVA-05, CVA-07, CVA-17 | W1 L5 | latent | 0 vault rows m; CVA-05 red |
| CVA-04, CVA-06, CVA-10, CVA-12, CVA-13, CVA-14, CVA-15, CVA-16, CVA-18 | W1 L5 | YAGNI | CVA-10 resolved W3: real (= W3RCT-23, red); DOC-20 retraction wrong |
| CVA-09 | W1 L5 | dropped | withdrawn |
| S1, S2, S3, S4, S5, S6, S7, S8 | W1 L5 | structure | |
| L3-04, L3-06, L3-07, L3-10, L3-11, L3-12 | W1 L3 | latent | 0 sync jobs live (m) |
| L3-13 … L3-26 (14) | W1 L3 | YAGNI | L3-18 blocked (paid API) |
| S1 … S10 | W1 L3 | structure | |
| T02, T03, T04, T05, T06, T07, T08, T09, T10, T11, T13, T14, T16 … T28 (13), N1 | W1 L7 | test-only | T02–T04, T17 blockers (TEST-CHANGE 0/13, pins, key literal, 12 suites uncollected) |
| T15 | W1 L7 | latent | fixture binds 0.0.0.0:8787, no consumer |
| DOC-03, 05, 06, 08, 09, 11, 12, 14, 15, 16, 18, 19, 21, 22, 23, 25, 26, 27, 28, 29, 31, 32, 33 | W1 L8 | doc | 7 REVIEW.md edits proposed, not applied |
| DOC-S1 … DOC-S10 | W1 L8 | doc | |
| DOC-04 | W1 L8 | dup | = DB-01 |
| DOC-34 | W1 L8 | latent | relay senders skip hold; 0 automations (m) |
| DOC-20, DOC-30 | W1 L8 | dropped | retracted R2 |
| W2DB-00, W2DB-12, W2DB-14 | W2 db | doc | W2DB-00 blocker (inv. 15 names) |
| W2DB-02, W2DB-03, W2DB-06, W2DB-07, W2DB-11, W2DB-13, W2DB-19 | W2 db | latent | 0135/0136 absent live; anon roles absent (m) |
| W2DB-09, W2DB-10, W2DB-15, W2DB-17, W2DB-18 | W2 db | structure | |
| W2SC-09, W2SC-10, W2SC-20 | W2 scope | structure | blockers (>1000 lines, dup helpers) |
| W2SC-14 | W2 scope | latent | cross-tenant over-fence, fails closed; 659ea29 y; 0 scope rows (m) |
| W2SC-15, W2SC-18, W2SC-19, W2SC-21 | W2 scope | latent | |
| W2SC-13, W2SC-16 | W2 scope | YAGNI | nit |
| W2SC-12, W2SC-17 | W2 scope | dropped | retracted R2 (17 = behaviour note) |
| W2S-13 | W2 sale | YAGNI | perf unmeasured |
| W2S-14, W2OR-06 | W2 sale/rt | latent | binding never read; hard guard m |
| W2S-17 … W2S-25 (9), W2S-29 | W2 sale | YAGNI | minor |
| W2S-26 | W2 sale | test-only | commercial judge untested (structure blocker) |
| W2S-28, S1, S2, S3, S4, S5, S6, S7 | W2 sale | structure | S1/S2/S4 blockers |
| W2OR-07, W2OR-13, W2OR-14 | W2 rt | latent | no route |
| W2OR-15, W2OR-16, W2OR-17 | W2 rt | YAGNI | nit |
| W2OR-S1, W2OR-S2, W2OR-S3, W2OR-S4 | W2 rt | structure | blockers |
| W2FA-18 | W2 fans-api | latent | blocker: absurd value rolls back takeover evidence; guard g |
| W2FA-09, W2FA-10, W2FA-12, W2FA-16, W2FA-20, W2FA-21, W2FA-22, W2FA-23, W2FA-27 | W2 fans-api | latent | 0136 absent live (m) |
| W2FA-24 | W2 fans-api | dropped | withdrawn R2 |
| W2FA-25, W2FA-26 | W2 fans-api | structure | history_import 1002 lines |
| TC-01 … TC-06, TC-08 … TC-21 (20) | W2 tests-c | test-only | TC-01..04 blockers; red `rwl/W2-tests-commercial:service/tests/test_w2_tests_commercial_collection.py` |
| TH-01, TH-03 … TH-21 (20) | W2 tests-h | test-only | TH-01 blocker |
| TH-02 | W2 tests-h | owner | OQ-9 |
| CS-01, CS-11 | W2 claims | latent | mock-wire spoof; widens W2FA-18 |
| CS-04, CS-09 | W2 claims | YAGNI | |
| CS-05, CS-08 | W2 claims | doc | commit bodies hide hold change |
| CS-07, CS-10 | W2 claims | test-only | |

## Orphans carried (no lane)
- worker.log `NativeProtocolError` 768 → 1,845 and `history_read_failed` 429 → 560 with 0 attempt rows (m, W1/W2 live-verify) — triage.
- Live checks still blocked: W1 C14, C16, C17, C18 (credential / login / plan_c_worker down); W2 V05 (needs write).

# W3 (a2f1dda6 + deltas, closed 2026-11-12)

- Source: `.scratch/rwl-W3/STATE.md`, 12 review lanes `.scratch/rwl-W3/<lane>/FINDINGS.md`, live `.scratch/rwl-W3/live-verify/RESULTS.md` (14 confirmed / 2 failed / 1 blocked, 0 OF calls). Programme OQs `.scratch/rwl-programme/STATE.md`.
- Pins: W3 = a2f1dda6 (same pin as W2 head) + delta-r0-live 659ea29..66a1da4a (R0 build) + delta-curl-80 PR #80 e8e90954..f5205ddd (unmerged).
- Live stack (m): DB `0133`, sending off (0 policies / scopes / intents); skip_list 0, blacklist 0, fans webhooks 0, model calls 0 → those checks vacuous. Live-confirmed: W3RCB-02 (X09: 26 open recovery rows, generation ≥50, max 1,676), W3RCB-03 = W3RO-02 (X14: `conversation_deferred` +18 / 10 min), W3RCT-14 (X16: healthy() True with 2,230 failures), W3DR-06 (X01: R0 head past live DB). W3RCB-01 partial (X13: :133 rising, :154 flat). W3RCA-10 route configured (X17: 401 not 503), echo unverified. X06 blocked; W3RO-01/05 not reached.
- Same rules as W1+W2 (severity = harm if reached; Reach = live|latent + guard + m/i/g). Groups G1/G2/G4/G5/G6 extended where the root cause is the same; G7–G12 new.

## W3 counts

- raw: 347 lane-local ids (CEA 31, DC 14, DR 22, FA 32, RCT 36, RCA 37, RCB 40, RD 32, RO 37, TH 15, TL 29, TSA 22) by grep of row / heading ids. Withdrawn, not counted: W3DR-21, W3RCB-T01 (+ runtime-content K16/K19/K22 labels).
- picked: 182 ids in 11 groups (extended G1 11, G2 10, G4 7, G5 28, G6 13; new G7 49, G8 16, G9 20, G10 12, G11 11, G12 5). rest: 165 = fix 56, YAGNI 95, dup-of 8, policy 6.
- live-confirmed: G7 only (m). G11 one route live-configured (X17). G12 in repo. All other groups latent behind sending off / 0 rows (m).
- red tests on W3 lane branches (THERMO): 47; none run against the live stack.

## W3 groups

### G1 (extended) · Ownership gap → re-offer of already-bought media
- same root cause as G1 (missing / pending / partial evidence reads as "not owned").
- ids: W3CEA-07 = W3RCB-08 (onlyfans_runtime/commerce.py:69 `_provider_evidence_held` counts `held` only → pending evidence passes final wire check; red `rwl/W3-conversation-engine-a:service/tests/test_w3_cea_pending_evidence_pg.py` 4/4); W3RCB-06 (ledger.py:255-268 `coverage_proven` = recent scan done, no full history); W3FA-01 (fans_api/events.py:286-308 held projection final, never resets); W3FA-04 (backfill.py:166-177 window cut = chat done); W3FA-12 (backfill.py:74-89); W3CEA-08 (commerce.py:71, :101-102 binding-only); W3CEA-09 (history_ownership.py:88 NULL message_id dropped); W3CEA-25 (engine.py:180-244); W3RD-05 (text_delivery.py:427-430 paid manual attachment skips gate); W3RCB-17 (launch_operations.py:170-171).
- live: latent — 0 evidence rows + sending off (m).
- dispute resolved: **CS-02 narrowed by W3CEA-07** — hold exists but only for `held`; pending/dismissed still pass (red m).
- fix: same G1 fix plus: any non-`held`-cleared evidence row (pending included) ⇒ `unknown` ⇒ refuse; coverage needs full-history scan, not recent window.
- OQ: OQ-8.

### G2 (extended) · Stop refusable; activate / config widens commercial scope
- same root cause as G2 (stop runs through validating config writers; widen paths skip `assert_enable_allowed`).
- ids: W3TSA-05 (launch_operations.py:127-128 stop refused on fingerprint change); W3RCA-02 (activation_status.py:295-301); W3RCA-03 (connections.py:353 pause refused + rolled back); W3RCA-15 (campaigns.py:254-256 cancel refused on revision); W3RD-06 (store.py:434-449 → pilot_scope.py:312-314 pause / no_contact refused on pilot fan); W3RCA-01 (activation_commands.py:204-254 second policy writer); W3RCA-S1 (four `OfDeliveryPolicy` writers); W3RCA-09 (activation_commands.py:131-132 no `fastt_scope` = every fan); W3CEA-14 (commercial_lifecycle.py:201-208 resume without validation); W3FA-15 (events.py:463 provider stop after projection commit). W2OR-04 new evidence: activate works on any account (m by reading).
- live: latent — 0 scope / policy rows (m).
- fix: G2 `stop_scope` (no validation, no revision check) used by every stop / pause / cancel / no_contact; one `policy_change(widen=…)` writer; missing fan scope ⇒ no fan.
- OQ: OQ-7, OQ-11.

### G4 (extended) · Unsellable media offered
- same root cause as G4 (no single per-asset readiness gate; sync defaults open).
- ids: W3RCT-01 (content_sync.py:357-362 `media_unusable` False when unknown — blocker); W3RCT-02 (content_sync.py:396-408); W3RCT-04 (content_sync.py:1450-1457 two readiness predicates); W3RCT-07 (content_sync_media.py:298, :332-354); W3RCT-28 (content_sync.py:351 window refusal → `unverified`); W3RD-02 (vault_management.py:876 vanished never re-checked); W3CEA-16 (catalogue.py:244, catalogue_select.py:108; blocker via W2S-05).
- live: latent — 0 vault / catalogue rows + sending off (m).
- dispute resolved: **W1 DOC-11 wrong** — catalogue.py:36 is an alias content-version check, not the readiness gate (W3CEA-16, m).
- fix: G4 gate; unknown media state ⇒ unusable; one readiness predicate shared by sync + `catalog_terms`.
- OQ: OQ-1..4, OQ-6.

### G5 (extended) · Paid-call ledger: unknown re-called, refusal → unknown, reservations bypassed
- same root cause as G5; R0 (66a1da4a) leaves W1-04 unchanged and fixes W1 F2.
- ids: W3TL-01 = W3DR-07 (replay re-calls `unknown`; probe red m); W3TL-02 (model_runtime.py:210-222 timeout unlocks same-pass fallback); W3TL-04 (`reconcile_unknown` → `settled`); W3TL-05 (= W1 F1 malformed Retry-After); W3TL-06 (= W1 F2, fixed in R0); W3TL-07 (llm_client 3× under one reservation); W3TL-08, W3TL-11 (profile_learning budget / brake bypass); W3TL-09 = W3DR-23 (max attempts config 3); W3TL-10 = W3DR-08 (= W1 F5 cursor starvation); W3TL-14 (vision reservation sized by base64); W3TL-18 (`handle(ingest_only=False)` skips `uncertain` filter); W3TL-23 (cap / reserve-before-wire untested); W3TL-24, W3TL-25 (provider POST without reservation); W3TL-27 (native `model-timeout` requeue); W3TL-13 (SQLite budget concurrency; disputed, g); W3DR-01 (openrouter.py:291-303 gzip decoded twice → every 200/429 `unknown`; blocker if provider compresses, g); W3DR-02 (crash between release and handover hides request); W3DR-14, W3DR-15, W3DR-19, W3DR-22; W3RCB-10 (native_worker.py:168-175 paid model call per inbound while capped); W3RO-14 (provider_model.py:91-94 legacy non-ledger calls extended).
- live: latent — 0 model-call rows (m); R0 not deployed (DB 0133 < R0 head, X01 m).
- disputes: W1 F2 "fixed" claim false at a2f, fixed in R0 66a1da4a (m); W1-04 latent not live (W3TL); W3TL-13 unresolved.
- fix: G5 fix plus: every provider POST takes a reservation (no optional budget); decode body once; release + handover in one txn; cap attempts at 2 in code, not config.
- OQ: OQ-14 (W3DR-02), OQ-21 (hold R0 deploy).

### G6 (extended) · Wrong price frozen / invented on a card
- same root cause as G6 (price copied ×4, no single checked source).
- ids: W3RD-01 (vault_management.py:452-454 manual product → `dynamic`, ladder reprices; blocker-conditional); W3RCB-14 = W3RO-11 (native_sales.py:113-130 ladder overwrites `terms["price"]`; = W1 DOC-09); W3RO-10 (quote.py:119-133 automatic 15/20 % discount; blocker-conditional); W3RO-09 (offer_import.py:978-1017 model-chosen category sets price); W3RO-12 (quote.py:439-447 `except Exception: return`); W3RCA-08 (configuration.py:258-333 shared-bearer editor sets prices); W3RD-10 (vault_editing.py:156-179 review ignores price); W3RD-28 (price copied ×4); W3RD-31, W3RD-32 (mutants: operator-price equality deletable); W3CEA-18 (claims.py:69-71 grounding from all grants); W3RCT-27 (content_sync.py:1468 rewrites approved item price).
- live: latent — 0 vault rows + sending off (m).
- fix: G6 fix plus: one price source (approved terms) read at send; ladder / discount only via an approved terms revision; manual product = `fixed`.
- OQ: OQ-1, OQ-16 (W3RO-10), OQ-20 (W3RD-01).

### G7 (new) · Failures retried forever or stuck forever; health stays green
- root cause: no durable per-item failure record (attempts, backoff, terminal give-up) and no expired-lease reclaim; one bad row / fan / 401 poisons the whole page or account loop, and health reads only "polled recently".
- ids: W3RCB-01 (native_connector.py:148 one bad row refuses 100-message page; native_protocol.py:133/:154) — blocker; W3RCB-04 (inbox_worker.py:111-118 identity 401 re-polled ~1 s) — blocker; W3RO-01 (recent_inbox.py:188-198 re-raise before `popleft` → account stuck on one fan) — blocker; W3RCB-03 = W3RO-02 (recent_inbox.py:187-197 retry every 60 s forever; red `rwl/W3-runtime-core-b:service/tests/test_w3_rcb_recent_inbox_retry_pg.py`, `rwl/W3-runtime-offers:service/tests/test_w3_ro_recent_inbox_pg.py`); W3RCB-02 (worker.py:108-128 recovery error ends account turn, lease kept); W3RCT-14 (inbox_health.py:13-16 healthy while failing); W3RCB-18 (heartbeat healthy when every turn refused); W3TSA-12 = W3RCT-10 = W3RD-07 = W3RCB-15 (fastt_manual.py:150-151, provider_connector.py:344-345: 401 / 429 → `unknown` instead of auth-failed / refused); W3RCB-05, W3RCB-11, W3RCB-12, W3RCB-13, W3RCB-16, W3RCB-22, W3RCB-24; W3RO-03, W3RO-04, W3RO-05, W3RO-18, W3RO-20, W3RO-21, W3RO-22, W3RO-23, W3RO-24 (= W1 F08, partly fixed), W3RO-25; W3RCA-06, W3RCA-12; W3RCT-05, W3RCT-06, W3RCT-08, W3RCT-09, W3RCT-13, W3RCT-16, W3RCT-18, W3RCT-19, W3RCT-21, W3RCT-22; W3RD-03, W3RD-04, W3RD-11, W3RD-12, W3RD-13, W3RD-29; W3FA-08, W3FA-09.
- live: **live-confirmed m** (X09 W3RCB-02, X14 W3RCB-03, X16 W3RCT-14; X13 W3RCB-01 one site rising). Rest latent i. Explains W1/W2 orphan `NativeProtocolError` / `history_read_failed` growth (i).
- fix (code judo): one `failure_ledger(lane, key)` row — attempts + next_at with exponential backoff, give-up after 5 → visible terminal state; parse per row, skip + record the bad one; identity 401 / 403 written onto the connection (stop polling); every lease query also reclaims `running` past `lease_until`; health = last success and open failure count, not last poll.
- OQ: OQ-10 (recent_inbox loop: leave running).

### G8 (new) · Fan / staff stop signals not honoured at send time
- root cause: opt-out / stop / takeover is a point check on one message in one sender with its own partial reason set; no single hold chain read by every automated origin.
- ids: W3CEA-01 (controls.py:98-100 any quote char disables opt-out detection); W3CEA-02 (controls.py:13-42 never reads global `blacklist`); W3CEA-03 (controls.py:22-25 skip_list subset); W3CEA-12 (engine.py:310-373 stop arriving mid-draft ignored); W3TSA-02 (conversation_policy.py:59-62 `ladder_stop` + `automated` passes; red `rwl/W3-trace-send-authority:service/tests/test_w3_tsa_ladder_stop.py`); W3RO-32 (stop-reason sets ×4); W3RO-06 (operator_cases.py:58-70 missing skip_list ⇒ no hold) — blocker; W3RO-07 (runtime_stop.py:69-88 pause overwrites stronger reason); W3RO-13 (stop case lifted by actor string); W3RCA-07 (safety_mode.py:13 detectors shadow by default); W3RCT-11 (engagement_worker.py:43-52 staff message doesn't hold nudges); W3RCT-12 (timed follow-ups bypass); W3TSA-09 (text_delivery.py:787-809, :1005-1030 holds 1/2 skipped for non-engine origins); W3TSA-14 (text_delivery.py:356-360 safety scan broad except); W3RCA-16, W3RO-35 (staff / imported rows misread). Red also `rwl/W3-conversation-engine-a:service/tests/test_w3_cea_custom_and_stop_red.py`.
- live: latent — sending off, 0 policies (m); X04 skip_list 0 / X05 blacklist 0 vacuous; X06 blocked.
- fix (code judo, IDEA-C): one `controls.reason(fan)` hold chain — canonical stop set, allow-list of non-blocking reasons, blacklist, opt-out over all inbound since last send — called at admit and consume by every automated origin; safety detectors enforce by default.
- OQ: OQ-12, OQ-17, OQ-18.

### G9 (new) · Account holds not uniform across senders (kill switch, gap, cap, concurrency)
- root cause: holds are optional callables per sender; native pilot / FASTT manual / FansAPI client / campaigns each read their own subset.
- ids: W3TSA-01 (native_pilot/runner.py:289-656 separate hold set; ledger.py:552-565) — blocker; W3TSA-03 (text_delivery.py:831, :875, :1093 send_gap never applied) — blocker; W3TSA-04 (text_delivery.py:1429-1440 kill switch only on FansAPI) — blocker-conditional; W3FA-02 (fans_api/protocol.py:251-274 public `send` outside holds) — blocker; W3FA-14 (real_transport.py:142-144 no pilot ⇒ `guard=None`); W3TSA-11 (concurrent_admission.py:145-147 cap probe error = 0); W3RCA-04 (daily cap one unit); W3RCA-05 (campaigns.py:353-358 lifetime only); W3RCA-14, W3RCA-19, W3RCA-22; W3RCB-09 (vault_upload_transport.py:112-122 real wall post); W3RCB-19, W3RCB-23; W3TSA-13, W3TSA-16, W3TSA-17 (`_signed_request` any POST), W3TSA-20; W3CEA-15, W3CEA-19.
- live: latent — 0 policy rows, sending off (m).
- fix (code judo): required `SendHolds` ctor arg (W3TSA-S3) with one `daily_cap_reason` + `send_gap` + kill switch; every wire POST goes through `consume_dispatch`; `_signed_request` GET-only, POSTs only via the allowlisted `_call`.
- OQ: OQ-11, OQ-12.

### G10 (new) · Cross-account / binding scoping gaps (inv. 8)
- root cause: lookups keyed by account (or nothing) where the row carries tenant + binding.
- ids: W3FA-03 (fans_api/events.py:218-227 `_resolve` by provider id, no account filter) — blocker; W3RO-36 (operator_cases.py:183-215 refused cross-tenant send still reads / writes); W3RO-27 (role_guard.py:34 account-less passes); W3RO-31; W3FA-07 (events.py:228-264 unknown `acct_` retires connection); W3RCT-03, W3RCT-15, W3RCT-17, W3RCT-25 (handover.py:24 `tenant_id=None` drops filter); W3CEA-21 (commerce.py:168-171 no binding filter); W3DC-03 (PR #80 ignores authenticated account); W3RD-30 (vault_management.py:444-459 replay match).
- live: latent — single account live, 0 webhooks (m); harm i.
- fix: every query by (tenant, account, binding); `None` never means "all"; refuse caller / row mismatch.
- OQ: none.

### G11 (new) · Credentials / key-shaped values exposed or persisted; direct egress
- root cause: provider bodies, ids and secrets copied raw into responses, logs and immutable rows; capture fetches skip the proxy transport.
- ids: W3RCA-10 (connections.py:110-129 → connection_api.py:71-83 `/capture-context` returns decrypted proxy URL); W3RCA-23 (activation_status.py:173-179 raw run error text); W3TL-03 (= W1 F3 key-shaped refusal evidence); W3TL-26 (llm_client keeps 500 chars raw body) — blocker; W3TL-28, W3TL-29 (raw provider strings / ids stored) — blocker-conditional; W3DC-01 (PR #80 `urllib` direct to OF CDN, session_bootstrap.py:522-527, :551-553) — blocker; W3DC-05, W3DC-06 (sign echoed in 400); W3RCA-20 (browser_capture.py:118-133 first capture direct); W3RCT-26 (content_sync_carrier.py:44-48 carrier skips family / budget).
- live: W3RCA-10 route configured live (X17 m), credential echo unverified; PR #80 unmerged (m); rest latent.
- fix: fixed error labels + sanitized log; never return decrypted secrets (return `configured: bool`); every OF fetch via the curl_cffi proxied transport.
- OQ: none.

### G12 (new) · Real-looking ids / creator name in the public repo
- root cause: fixtures and recovery scripts copied from live data.
- ids: W3RCB-07 (monitor_incident_recovery.py:77-81) — blocker; W3RO-08 (moxy_replacement/sources/account-bindings.v1.json; offer_import.py:851-868) — blocker; W3TL-22; W3TH-09 (test files share the literals) — blocker; W3TH-10 (fake_fans_server.py:10 reads live creator env).
- live: present in repo HEAD (m by the lanes); values not repeated here.
- fix: replace with synthetic ids; load real bindings from host-only config; history scrub = owner call.
- OQ: OQ-15.

## W3 rest

| ids | verdict | note |
|---|---|---|
| W3CEA-04, W3CEA-11, W3CEA-22, W3CEA-23 | fix | customs claim check; "named human" deny-list; None deref → 5xx; RuntimeError in settle (g) |
| W3CEA-05 | dup-of | W1 S1 (R11 / R13 no rule); red `test_w3_cea_rules_denylist_from_c2c09b01.py` |
| W3CEA-06, W3CEA-13 | fix | tests pin takeover hold (mutants survive); claims suites uncollected |
| W3CEA-10 | policy | = W3TSA-19, OQ-13 |
| W3CEA-17, W3CEA-20 | YAGNI | minor |
| W3CEA-24 | fix | doc |
| W3CEA-S1 | dup-of | W3TSA-S1 |
| W3CEA-S2 … W3CEA-S6 | YAGNI | structure |
| W3DC-02, W3DC-04, W3DC-07, W3DC-08, W3DC-12, W3DC-13, W3DC-14 | fix | PR #80: fix before merge (tautological verify, cookie header, path input, unbounded read, 0 tests) |
| W3DC-09, W3DC-10, W3DC-11 | YAGNI | |
| W3DR-03, W3DR-05 | fix | disclosure: 0 `TEST-CHANGE:`; migration change unstated |
| W3DR-04 | policy | network failure joins incident recovery |
| W3DR-06 | policy | OQ-21: R0 cannot boot on live DB 0133 (X01 m). → G13 (W4, not recounted) |
| W3DR-09 | dup-of | W1-20 (worse) |
| W3DR-11, W3DR-17, W3DR-18 | fix | Dockerfile outside §3; tests uncollected; downgrade guard |
| W3DR-10, W3DR-12, W3DR-13, W3DR-16, W3DR-20 | YAGNI | structure / counter / test env |
| W3FA-05, W3FA-16 | fix | unsupported verified events dropped; late `connected` flips auth state |
| W3FA-06 | fix | `release_send_block` no production caller (fails closed). **W1 fans-api M1 wrong** |
| W3FA-10, W3FA-11 | YAGNI | no prod caller / fails closed |
| W3FA-17, W3FA-19, W3FA-20 | fix | doc |
| W3FA-21, W3FA-22 | fix | tests pin kill-switch default / verify (→ W3TH-04) |
| W3FA-S1 … W3FA-S12 | YAGNI | S3 code judo: one importer, delete backfill |
| W3RCT-23 | fix | Misc withdrawal deletes operator memberships; red `rwl/W3-runtime-content:service/tests/test_w3_runtime_content_misc_withdrawal.py`. = CVA-10 → **W1 DOC-20 retraction wrong** |
| W3RCT-24 | policy | OQ-19 provisioning defaults live (X03 inert m) |
| W3RCT-20 | YAGNI | repair >1000 no paging |
| W3RCT-T1 | fix | fan-messaging scheduler has 0 tests |
| W3RCT-T2, W3RCT-S1 … W3RCT-S6 | YAGNI | structure / no caller |
| W3RCA-11 | dup-of | W3RCA-S2 (`Refused` by message text) |
| W3RCA-13, W3RCA-17, W3RCA-18 | fix | replay `applied`; window lower bound; GET 503 |
| W3RCA-21, W3RCA-D1 | fix | doc |
| W3RCA-S2 | fix | `Refused(code=…)` — removes text-matching under G7 / W3RD-23 |
| W3RCA-S3 … W3RCA-S11 | YAGNI | S11 ideas: delete until owner asks |
| W3RCA-T1, W3RCA-T2 | fix | stale head pin; PR CI skips `_pg` |
| W3RCB-20 | fix | broad except → `draft_ref` |
| W3RCB-21, W3RCB-25 | YAGNI | |
| W3RCB-S01 … W3RCB-S14 | YAGNI | structure |
| W3RCB-D01 | fix | doc |
| W3RD-08, W3RD-09, W3RD-15 | fix | exclusive lock on GET; materialize compare; Misc filing silent |
| W3RD-14 | YAGNI | clock skew window |
| W3RD-16 … W3RD-25, W3RD-27 | YAGNI | structure (text_delivery 1588 L, store 1033 L) |
| W3RD-26 | dup-of | W3RCA-T2 |
| W3RO-16, W3RO-30, W3RO-37 | fix | Moxy path skips price bounds; duplicate single; truncated JSON |
| W3RO-15, W3RO-17, W3RO-19, W3RO-26, W3RO-28, W3RO-29, W3RO-33, W3RO-34 | YAGNI | structure / phrase list |
| W3TH-01, W3TH-02, W3TH-03, W3TH-04, W3TH-06, W3TH-12, W3TH-14 | fix | CI gate / runner false green; wire fakes swallow invariants |
| W3TH-05, W3TH-07, W3TH-08, W3TH-11, W3TH-13, W3TH-15 | YAGNI | |
| W3TL-12 | dup-of | W1-20 |
| W3TL-15 | dup-of | W1-12 |
| W3TL-16 | dup-of | W1-21 |
| W3TL-17, W3TL-19 | YAGNI | |
| W3TL-20, W3TL-21 | fix | REVIEW #6 list; 422 test gap (W1 T06) |
| W3TSA-08 | fix | mutant M5 (delivery_block at consume) survives |
| W3TSA-15 | policy | stop after consume can't recall in-flight POST (bounded) |
| W3TSA-18 | fix | discounted resend: DELETE ok, POST 4xx → no card |
| W3TSA-19 | policy | OQ-13 |
| W3TSA-S1 … W3TSA-S5 | YAGNI | S1 / S3 are the G8 / G9 code judo |

## W3 disputes resolved
- CS-02 narrowed by W3CEA-07 (G1).
- W1 DOC-11 wrong (W3CEA-16, G4).
- W1 DOC-20 retraction wrong: CVA-10 real (W3RCT-23 red).
- W1 fans-api M1 wrong (W3FA-06).
- W1 F2 "fixed" false at a2f; fixed in R0 66a1da4a (W3TL-06).
- W1-04 latent, not live (W3TL). Open: W3TL-13.

# W4 (a2f1dda6 + delta 3b8fc64c, closed 2026-11-12)

- Source: `.scratch/rwl-W4/STATE.md` (Threads, Summary), 11 review lanes `.scratch/rwl-W4/<lane>/FINDINGS.md`, live `.scratch/rwl-W4/live-verify/RESULTS.md` (13 confirmed / 2 failed / 0 blocked / 3 n/a, 0 OF calls). Programme `.scratch/rwl-programme/STATE.md` (OQ-21b…26, Disputes, Fable 11:2x).
- Pins: W4 = a2f1dda6 (kept) + delta-r0-decoding 66a1da4a..3b8fc64c. Running build (m, live-verify step 0 + end, no restart): `8d0001f9` jaka/takeover-20261111 (merge of #82 c48eb755), clean, schema HEAD 0133 = engine DB; contains 659ea29 + e8e90954, not a2f1dda6. :8787 = `onlyfans_app` (engine API on PG); legacy `server.py` relay + SQLite not running → relay-only findings n/a on the running stack (m), reachable on any relay deploy (deploy-here.sh / deploy-vps.sh) (i).
- Live (m): engine DB 0133 with 0129b fully applied (DS-1, MB-3); vault-frozen + reconnect guards present (DS-2); `fastt_runtime` schema absent on engine DB (DS-3 failed vs lane's 16/0); 0 RLS policies on `of_%` (W4TI-03 live); 0 pending fans projections (SS-1); 1 live binding, 0 dup (OFR-3); 0 paid messages (OFR-2, vacuous); downgrade evidence all 0 (MB-1, vacuous); attribution view present on PG (MA-1); 0 pre-0032 NULL-kind automation sends (MA-2); 0 command-route 5xx since restart (MT-1); 187 inbound messages match the lane's minor-adjacent regex (count only; RESULTS check id `W4CEC-1` measures W4CEC-4 reach; some benign, g). migrations-b-2 failed = stale lane expectation (W4MB-08 not shown live).
- Same rules as W1–W3. W4 lanes mark nearly every id `659ea29: y`; the earlier "present on 8d0001f9 (inferred)" is replaced by measured values in **W3+W4 reach @ running 8d0001f9** below (blockers/majors).

## W4 counts

- raw: 211 lane-local ids (DD 7, SS 10, TI 7, CEB 28, CEC 20, DM 29, DS 21, MA 22, MB 20, MT 18, OFR 29) by grep of `W4<lane>-NN`. Withdrawn, not counted: W4DD-04, W4DM-27 → 209.
- picked: 127 ids in 12 groups (extended G1 4, G2 4, G5 13, G6 2, G7 4, G8 13, G9 2, G10 23, G11 9, G12 4; new G13 45, G14 4). G3 / G4: no W4 adds. rest: 82 = fix 43, YAGNI 34, dup-of 2, policy 3.
- live-confirmed: G10 W4TI-03 (doc-only harm); G14 reach (187 matching inbound; sending off). G13 latent on the running stack (DS-3 failed; relay SQLite not running); W3DR-06 condition still true (R0 HEAD 0129b vs live 0133, i from DS-1). All else latent behind sending off / 0 scope, policy, model-call rows (m).
- red tests on W4 lane branches (THERMO): 22; none run against the live stack.

## W4 groups

### G1 (W4 adds) · Ownership gap → re-offer of already-bought media
- same root cause (several ownership proofs; unknown / partial read as not owned).
- ids: W4CEB-4 (ownership_gate.py:19-31 vs ownership.py:177-183: free/NULL `resolved_by` counted mapped, never owned; `media_count` never compared); W4OFR-05 (onlyfans_read/repo.py:1017, :1037-1048 empty `media_ids` → message id reported as owned media id; red `rwl/W4-onlyfans-read:service/tests/test_w4_onlyfans_read_contract.py`); W4OFR-29 (projections.py:255-258 vs repo.py:1003-1005 timestamp vs `is_paid`); W4DM-14 (backfills/backfill_relink_ppv_messages.py:8-12, :137-145 guessed relink lifts paid-unlinked hold).
- live: latent — 0 vault / catalogue rows, 0 paid messages (OFR-2 m), sending off.
- fix: G1 fix + one canonical paid/owned proof shared by gate, read API and history; `len(ids) < media_count` ⇒ unknown; never invent media ids.

### G2 (W4 adds) · Stop refusable / false success
- same root cause (stop path reads + validates scope config; replay without current state).
- ids: W4CEC-1 — blocker (store.py:434-436 → pilot_scope.py:293-302 → scope_contract.py:249-256: stored scope config failing `validated()` refuses every pause / no_contact / takeover on the account, incl. engine opt-out + webhook takeover, while campaigns keep sending); W4CEB-10 (operator_v2.py:405-406 stop/takeover refused without owner row); W4MT-3 (launch_operations.py:52-58, :123-125 stop replay after resume reports stale "stopped"); W4MT-10 (launch_api.py GETs lock runtime `FOR UPDATE` → queued stop → lock_timeout → 500).
- new evidence: W2OR-04 worse — connection activate re-enables a runtime stopped by pause / emergency_stop (connection_api.py:106 → connections.py:308-351, m); W2OR-18 stop → 400 "needs correction" (m); W3TSA-05 fingerprint refusal deletable (W4MT-6 M2 survived 56/56).
- live: latent — 0 scope / policy rows (m).
- fix: G2 `stop_scope` never reads/validates scope config; stop allowed without owner row; replay returns `replayed:true` + current state; read routes `lock=False`.
- OQ: OQ-7, OQ-11.

### G5 (W4 adds) · Paid-call ledger: release / unknown / reservation
- same root cause (classification + reservation lifecycle owned by several modules; `release` on unproven refusal).
- status: W3DR-01 FIXED by 3b8fc64c (m: W3 probes 3/3, team suites 17 red → 22 green; R0 line only, not on 8d0001f9). Still red at 3b8fc64c: W3DR-02, W1 F5, W3DR-07 ×2 (m). W3TL-23 closed as test-gap (new green test). W3TL-04 latent-dead (`reconcile_unknown` no prod caller, m; dispute W4MT, Sol agrees).
- ids: W4MT-1 (openrouter.py:9-10, :274-276; pilot_supervised.py:352-358: any non-200 incl. 502/504 released at cost 0 → re-planned; present at 3b8fc64c); W4CEB-1 (model_ledger.py:266-343, model_runtime.py:58, :187: settle on peer-swept `unknown/lease_expired` keeps outcome → paid answer never reused, fallback pays again; red `rwl/W4-conversation-engine-b:service/tests/test_w4_ceb_lease_lapse_settle_pg.py`); W4CEB-14 (lease guard assumes total deadline; httpx per-phase); W4CEB-17 (replayed failures billed without fix); W4CEB-19 (fallback + MAX_ATTEMPTS=1 → refused, no handover); W4SS-4 (model_runtime.py:248 recheck before reserve/mark_calling locks); W4MT-9 (pilot_supervised.py:307-318 lease sweep misses old `calling` rows); W4MT-18 (openrouter.py:125-128 cost overflow → unknown w/o generation id); W4DD-03 (openrouter.py:294-295 200 body unbounded, decompression bomb); W4DD-06 (non-200 2xx/3xx released); W4CEC-11 (draft-only scope skips `planning_block` → paid call per inbound); W4MA-05 + W4DS-09 (`grok_daily_cost` nullable `account_id` in PK: SQLite upsert never conflicts → cap never runs, m M5; PG insert fails).
- live: latent — 0 model-call rows, model off (m).
- running 8d0001f9: W4MT-1 **y** (m: pilot_supervised.py:351-352 `^http_[0-9]{3}$` = proven_refusal; openrouter.py:9-10 blob = a2f1dda6).
- **dispute OPEN: W4MA-05 vs W4DS-09** — does `llm_client._reserve` (:740, called :1075) ever get `account_id=None`? live writer m (W4MA), None caller unproven (g). Owner: **W5 llm-profile-learning**.
- fix: G5 fix + only 4xx (and proven refusals) release; 5xx after wire start ⇒ unknown; settle from `unknown` resets outcome; total deadline + 1 MB cap + `Accept-Encoding: identity`; cap row keyed NOT NULL (sentinel).
- OQ: OQ-14, OQ-21.

### G6 (W4 adds) · Wrong price / invented discount
- ids: W4CEB-7 (model_output.py:149-158, :383-399 discount vocabulary misses "half off" / "2 for 1"; denial exemption with contrastive "but"); W4MA-08 (20260611_0100_automation_kind.py no backfill × ai_chatter.py price-band query: pre-0032 automation sends counted as human paid asks → price bands; MA-2 = 0 on engine PG, relay DB not read).
- related: W4MB-06 Moxy price-0 branch (in G13).
- live: latent — sending off; relay not running (m).
- fix: judge concept match for discounts; backfill `automation_kind` or exclude the Automation sentinel.
- OQ: OQ-16.

### G7 (W4 adds) · Stuck forever; health green
- ids: W4CEB-2 (operator.py:78-92, model_config.py:92: default handover target never a valid actor → `handover_pending` forever); W4CEB-21 (operator_v2.py:325-336, :650-652 `unresolved` caps drop items silently); W4OFR-03 (health.py:57-59 verified connection ignores `expires_at` → `needsAttention` false); W4OFR-04 (repo.py:815-816, :766 quarantined fan with no state row invisible everywhere).
- live: latent (i; 0 handovers / quarantines not measured).
- fix: G7 fix + handover target = validated actor or queue; backlog marker per capped kind; health reads expiry + binding; list fans by quarantine as well as state.

### G8 (W4 adds) · Fan / staff signals not honoured: stop, newer message, takeover
- same root cause, widened: no single "newer input" / hold predicate (≥8 copies, W4SS-3) read by every origin.
- ids: W4SS-1 — blocker (approval.py:110-122; pilot_scope.py:373-374; events.py:100-145, :336-337: accepted-but-unprojected fan message invisible → older approved draft sends when no pilot scope; red `rwl/W4-trace-supersede:service/tests/test_w4_trace_supersede_seams_pg.py::test_w4ss1_*`); W4SS-3 (8 divergent newer-input checks); W4SS-5 (pending branch no direction/binding filter → cancels instead of defers); W4SS-8 (campaigns.py:86-124, :338-435 send over unanswered inbound, kill engine draft; OQ-22); W4SS-9 (launch_engagement/readers.py:16-21 no binding filter); W4CEB-5 (journey.py:587-590, :633-641 "maybe later" overwrites opt-out → lapses to open); W4CEB-9 (pilot_ingress.py:27-32 catch-up ingress dated before scope dropped → stop unclassified); W4CEB-13 (journey.py:604-608 silent continue); W4CEC-2 — blocker-cond (pilot_scope.py:427-430, :391-407: trial exception lifts `human_sent` hold, held deletions, chat-queue digests; OQ-24); W4CEC-3 (exception never checks fingerprint, no expiry); W4CEC-14 (absence-as-proof deletion; non-numeric id wedges account); W4MT-2 (pilot_supervised.py:525-526 kill switch off + restart ⇒ no webhook intake, fan stops lost); W4MT-8 (pilot_draft.py:111-112 validate before intake → 409, inbound lost).
- live: latent — sending off, 0 scope rows; SS-1 0 pending projections (m).
- fix (code judo): one `newer_input(s, runtime, fan, epoch)` (pending OfIngress + pending projections + revision, binding-scoped) inside the G8 `controls.reason(fan)` chain; intake always before validate; exception = takeover only, fingerprint-bound, `valid_until`; pause kind stored durably.
- OQ: OQ-12, OQ-17, OQ-18, OQ-22, OQ-24.

### G9 (W4 adds) · Account holds not uniform
- ids: W4CEB-3 (operator_v2.py:968-987, :1054-1056 any non-holding digest with same key proves a deletion → lifts account-wide `pilot_evidence_pending`; one key reusable); W4DS-13 (onlyfans_pilot_models.py:472-474, :329-345, :388-401 lease key incl. tenant, receipts w/o account → cap by caller locks only).
- live: latent — 0 scope rows (m).
- fix: G9 `SendHolds`; proof = exact type + message id + recipient, one-use; receipts carry account + FK.

### G10 (W4 adds) · Cross-account / binding scoping (inv. 8)
- same root cause (keys by account or nothing; DB enforces none: single-column FKs, RLS without policies).
- ids: W4TI-01 (pilot_supervised.py:100-140 foreign provider event stored + holds pilot fan; red `rwl/W4-trace-isolation:service/tests/test_w4_trace_isolation_pilot_foreign_provider_pg.py`); W4TI-02 (single-column binding/item FKs, onlyfans_engine_models.py:22 … :493 + migrations); W4TI-03 (RLS w/o `CREATE POLICY` — **live-confirmed m**, doc-only harm); W4TI-04 (two resolvers; `None` = any account); W4TI-06, W4TI-07 (mutants survived: no shared-fan-id / rebind tests); W4DM-06 (mass_message_funnels no owner, relay multi-user); W4DM-09, W4DM-10 (`uq_event_dedupe` w/o account), W4DM-12 (Blacklist global, OQ-W4DM-a), W4DM-29; W4DS-10, W4DS-11 (`command_key` global), W4DS-12 (model_call fan not tied to request), W4DS-14; W4MA-11 (chatter grants: last row deleted ⇒ full access); W4MA-14; W4OFR-06 (read router on relay without AgencyScopeMiddleware; not on running app, OFR-1 n/a m); W4OFR-18 (authz and read in different sessions); W4OFR-26 (no cross-tenant HTTP test); W4OFR-28 (`list_bindings` / `get_binding` no channel filter); W4CEB-16 (operator_v2.py:597-601); W4MT-6 (M1: configuration_api.py:261 ownership route's only tenant check deletable, 69/69 green). New evidence: W3FA-03 red 2/2 (`rwl/W4-trace-isolation:service/tests/test_w4_trace_isolation_resolve_pg.py`); W2SC-14 narrowed.
- live: no live cross-account path (m, W4TI + W4OFR); 1 binding live (OFR-3 m).
- fix: G10 fix + `UNIQUE(account_id,id)` on bindings + catalog_items, composite FKs; one `resolve_connection(..., tenant, account)` scope required; two-account same-fan-id tests; relay single-tenant decision or owner columns.
- OQ: OQ-W4DM-a (relay single-tenant?) → owner.

### G11 (W4 adds) · Credentials exposed / at rest
- **OQ-26 secrets at rest:** W4MB-02 (20260820_0100_user_llm_keys.py:45; tenant_keys.py:19-20 agency LLM keys plaintext); W4DM-05 (import_legacy.py:75-97, :162-175 via server.py session bootstrap + admin mutations: cookies + plaintext proxy password into SQL each capture); W4DM-28 (mirror never deletes → deleted proxy / account creds stay); W4DM-15 (models.py:2680, :2768, :2802 user / chatter passwords + invite token plaintext); W4MA-10 (0022/0200 migrations, auth.py:519, :540 plaintext compare `!=`; OQ-W4MA-a); W3RCA-10 (W3, not recounted: capture-context decrypted proxy URL).
- other: W4MA-18 (env.py:68 query-string passwords printed); W4MT-11 (content_sync_api.py:89-93, configuration_api.py:101, :147 raw `str(exc)` to client); W4OFR-14 (raw DB text to wire, latent); W4DS-06 (snapshots 0081 / 0087 `sealed_config` tables no RLS / REVOKE).
- live: relay-only items not on running stack (m, n/a); W3RCA-10 route configured (W3 X17). W4DM-05/-28 not Live-risk (Fable 11:2x: server.py unreachable from running app).
- fix: G11 fix + hash passwords / invite tokens (KDF, rehash on login); encrypt keys + proxy creds under host-only key; stop mirroring JSON creds into SQL (or reconcile deletes).
- OQ: OQ-26 (priority = owner), OQ-W4MA-a / OQ-W4DM-b (plaintext decision vs public-IP default deploy).

### G12 (W4 adds) · Real-looking ids / names in the public repo
- ids: W4MB-01 (20260805_0100_customs_owed_column.py:13-14 account/fan id pair + fan nickname; 20260819_0200_cotag_brain_settings.py:11-12); W4MA-12 (20260524_0100_employees_per_user.py:54 username drives backfill; 20260522_1208:14-15 tx hash; 0053/0054 persona text); W4DM-25 (models.py:1627-1631 personal-looking default handle + stale contracts); W4DS-16 (db/repo.py:54 creator handles; 6/9 functions dead).
- live: present in repo HEAD (m by lanes); values not repeated here.
- fix: synthetic placeholders; history rewrite = owner.
- OQ: OQ-15.

### G13 (new) · Schema line / upgrade path
- root cause: three schema sources (init_db `create_all` + catch-up, relay alembic line whose 0001 = live-ORM `create_all`, engine snapshots + `check_schema` digest pins) with no single path from any existing DB to HEAD; DB guards only as strong as the pin, not tested.
- (a) R0 / engine line: W3DR-06 (W3, not recounted: R0 HEAD 0129b refuses live 0133; no schema.py-only fix, W4DS m) + **OQ-21b** (default (a) rebase R0 onto the 0133 line; (b) downgrade only if MB-1 zeros — measured 0,0,0,0,0, vacuous; (c) stamp = never); W4DS-01 (16 `fastt_runtime` tables outside alembic + check_schema, 3 drifted DDL copies; DS-3: schema absent on engine DB → latent there); W4DS-02 (operations.py:32, :129-142 pin can't back up 0133 before 0133→0136; no app-level way back); W4DS-03 (unknown `onlyfans_since` tag silently skipped); W4DS-04 (guard pins ignore tgattr / proconfig / prosecdef); W4DS-05 (vault-frozen + reconnect guards not verified at boot; DS-2 present live m); W4DS-07 + W4MB-08 (snapshots 0089 / 0090 edited in place after commit; MB-2 stale expectation); W4MB-13 (revisions build tables from live ORM).
- (b) relay SQLite line: W4DM-01 (engine.py:211-232 catch-up `DEFAULT CURRENT_TIMESTAMP NOT NULL` on populated table → init_db raises, relay serves 500s; m sqlite repro); W4DM-02 (unique-index catch-up failures swallowed); W4DM-03 (non-role relay on PG URL runs create_all on engine PG); W4DM-04 + W4MA-01 (documented `alembic upgrade head` on self-healed DB always fails at 0002; 14 non-idempotent revisions, m) + W4MB-14 (0059/0063/0064 same); W4DM-07 (Moxy intent FK missing on create_all DB); W4DM-16; W4DM-21 (catch-up untested, mutant survived); W4MA-02 (0001 not reproducible); W4MA-03 (attribution view only via migrations → fresh init_db 0 views → stats 500; MA-1 present on PG); W4MA-04 (0006 / 0026 silently NULL account + relabel provider if forced, m); W4MA-06 (55/55 downgrades destructive; `downgrade base` 106 → 17 tables, m) + W4MB-07; W4MA-07 (env.py cwd default creates/targets wrong DB, m); W4MA-09 (bought_amount drop, manual preservation); W4MA-13, W4MA-15 (Automation sentinel only via migration, 0 on init_db, m), W4MA-16, W4MA-17, W4MA-19, W4MA-21 (no suite runs relay revisions).
- (c) DB guards weaker than invariants (rule: all W4MB here): W4MB-03 (0129b unknown guard survives 111 tests once pin re-hashed, m) + W4DS-08 (settled CHECK → `true` survives, m); W4MB-04 (model-call INSERT unguarded), W4MB-05 (decision `unknown→held→queued` bypass), W4MB-12 (generation N+1 while N unknown) — inv 5; W4MB-10 (approval actor / expiry not frozen) — inv 7; W4MB-06 (Moxy branch: paid item at price 0, ≈ G6); W4MB-09 (`of_fans_evidence`, `of_ledger_exclusion`, policy-change no append-only guard); W4MB-11 (two live grants per offer); W4MB-15, W4MB-16, W4MB-17, W4MB-18, W4MB-19, W4MB-20 (PG-only rebuild, JSON rename, unnamed drops (W2DB-00 class), CHECK mismatch, snapshot runner ×10, guard looseness).
- live: **latent** on the running stack — engine PG 0133 = schema HEAD 0133 (m); `fastt_runtime` absent (DS-3); relay SQLite not running (MA-3, DM-1/-2 n/a m). Relay items live on any fresh / existing relay deploy (i).
- fix (code judo): relay — init_db is the only SQLite path (stamp a `selfheal` sentinel, docs drop the alembic step, catch-up per column with constant defaults, unique-index failure = boot error, seed sentinel + view in init_db); engine — roll-forward only (every downgrade raises), snapshot sha256 asserted, `fastt_runtime` under alembic + check_schema; one `_pg` test per DB guard (insert/update the forbidden transition → raise).
- OQ: OQ-21, OQ-21b.

### G14 (new) · Content safety
- root cause: safety / judge regexes run on un-normalised text with a fixed contextual exemption list; no self-stated-age rule; safety tests not collected.
- ids: W4CEC-4 (safety_context.py:8-15, :32-38; callers model_output.py:87, :93, journey.py:189: "schoolgirl" / "stepsis" / "shy teen" roleplay passes both checks + judge unless the fixed context word appears; red 7/7 `rwl/W4-conversation-engine-c:service/tests/test_w4_conversation_engine_c_safety.py`); W4CEB-6 (model_output.py:354, :491-493 "I’m 17" (curly) / "I'm only 17" / "seventeen" pass age gate); W4CEB-8 (curly apostrophe normalised in one place only; refusal / human-claim / `_LATER` regexes on raw text); W4CEC-16 (safety_context / prompt_registry / recovery / sales tests nested, not collected).
- live: reach **187** inbound messages match the minor-adjacent regex (m count, no text read; some benign, g); harm latent — sending disabled (m).
- fix: never exempt schoolgirl/boy, teen, step-* terms; normalise once (NFKC, ’‘→', zero-width strip) before every judge regex; any self-stated age < 18 (digits or words) ⇒ hard stop + human handover; move gating tests top-level.
- OQ: **OQ-25** (ask owner, with OQ-17); default hard stop, blocker before any autonomous send.

## W4 rest

| ids | verdict | note |
|---|---|---|
| W4DD-01 | YAGNI | structure (Sol rejected defect) |
| W4DD-02, W4DD-05 | fix | openrouter table doc; decode bound test for gzip/br |
| W4DD-07 | policy | OQ-W4DD-a: `sk-` synthetic fixtures under REVIEW #1? default allowed |
| W4SS-2 | fix | R04 can't go red; per-seam tests added (kill M1/M2) |
| W4SS-6 | fix | doc: staff-msg cancel located, lazy |
| W4SS-7 | policy | consume_dispatch → POST window; dispute Sol blocker vs lane minor; name boundary in inv. 7 |
| W4SS-10 | fix | stale refusal counts vs call budget; newest msg gets no draft |
| W4TI-05 | dup-of | W1-15 / W3RCT-25 (Sol rejected as new) |
| W4CEB-11, W4CEB-12 | YAGNI | structure: inert journey knobs (delete); identity-line precedence ×2 |
| W4CEB-15, W4CEB-18, W4CEB-20, W4CEB-24 | fix | 2nd approve audit row; ack not in txn; None profile → AttributeError; permissive duck-typed defaults |
| W4CEB-22 | fix | judge quantity claims ("10-pic set") |
| W4CEB-23, W4CEB-25, W4CEB-26, W4CEB-27 | YAGNI | dead `prefixed`; dup rule tables / keys; fixtures in prod |
| W4CEB-28 | fix | doc |
| W4CEC-5 | fix | tip asks reach prompt examples (inv. 12); red 5 in same file as G14 |
| W4CEC-6, W4CEC-7, W4CEC-8, W4CEC-9, W4CEC-17, W4CEC-18 | YAGNI | structure; CEC-7 = route readers via `scope_contract.load` (W2SC-10 judo) |
| W4CEC-10, W4CEC-13, W4CEC-15, W4CEC-19 | fix | fail-closed mislabels (draft cancelled, card price on text-only, credential hold hides cause, refusal reason lost) |
| W4CEC-12, W4CEC-20 | fix | prompt injection via history / example lines; one untrusted-text helper |
| W4DM-08, W4DM-17 | fix | `sqlite_where`; closed-set CHECKs |
| W4DM-11 | fix | `AutomationRule.is_enabled` default True (inv. 4); relay |
| W4DM-13 | fix | FanSafetyFlag "append-only" vs CASCADE |
| W4DM-19 | policy | fan IP / UA retention |
| W4DM-18, W4DM-20, W4DM-22, W4DM-23, W4DM-24, W4DM-26 | YAGNI | nit / structure; DM-20 delete script; DM-22 split models.py after freezing 0001 |
| W4DS-15, W4DS-17, W4DS-19, W4DS-20 | YAGNI | structure (check_schema 370 lines; mixins; 3 sources of truth) / nit |
| W4DS-18, W4DS-21 | fix | assert metadata table set; mismatch msg names revisions |
| W4MA-20 | fix | doc (claims ≠ code ×8) |
| W4MA-22 | YAGNI | view SQL owner (fix with W4MA-03) |
| W4MT-4, W4MT-7, W4MT-13, W4MT-14, W4MT-16 | YAGNI | structure: auth gate ×14, tick 228 lines, private imports, v1/v2 both mounted |
| W4MT-5 | fix | 53/57 broad excepts unlogged (MT-1: 0 5xx live) |
| W4MT-12 | dup-of | W3RCA-S2 (`Refused` by message text) |
| W4MT-15, W4MT-17 | fix | vault cancel no evidence + rmtree before commit; doc |
| W4OFR-01, W4OFR-02, W4OFR-07, W4OFR-16, W4OFR-17 | fix | money views disagree (spend vs earnings vs financials; limit before filter, red `test_w4_onlyfans_read_earnings_pg.py`; stale totals; reasons overwritten) |
| W4OFR-08, W4OFR-09, W4OFR-12 | fix | red `test_w4_onlyfans_read_contract.py` (non-ASCII bearer 500; Unavailable TypeError; fan id aliases) |
| W4OFR-10, W4OFR-11, W4OFR-13, W4OFR-19 | fix | silent broad except / no degrade / outcome coercion / swallowed facts error |
| W4OFR-15, W4OFR-20 | fix | doc (millicents mislabel; refusal-vs-empty) |
| W4OFR-21 | fix | test-gap: fake repo hides batched paths |
| W4OFR-22, W4OFR-23, W4OFR-24, W4OFR-25, W4OFR-27 | YAGNI | structure; OFR-22 = G10 judo (one scoped read) |

## W3+W4 reach @ running 8d0001f9 (blockers/majors; minors/nits skipped)

- Method (m): per id, cited files at the lane pin (a2f1dda6; delta lanes 66a1da4a / f5205ddd / 3b8fc64c) vs `git show 8d0001f9:`. y = blob identical, or cited lines not in the 8d0001f9→pin `git diff -U0` hunks, or mechanism found by `git grep`; n = mechanism file/code absent. Only W2 commits (e8e90954..a2f1dda6) and R0 (66a1da4a, 3b8fc64c) are missing from the build; W2 touched text_delivery.py only at :257-287.
- Count: 190 ids · y 186 · n 4 · ? 0. Severity-unlabelled rows taken from lane text (W3RCB-08 = W3CEA-07; W3TSA-02/-04/-09, W3FA-02, W3RCB-07 blocker[-cond]; W3TSA-11, W4TI-02/-06, W4DS-01 major); skipped W3TSA-17 (minor), W4TI-03 (info).
- Judgement y (m by reading): W4CEC-1 via own `pilot_scope.current` validate / fingerprint refuse (pilot_scope.py:281-294 → store.py:434-436; scope_contract.py absent); W4MT-6 M2 only (launch_operations fingerprint refusal; M1 configuration_api route is W2); W3TSA-05 (`control_account` fingerprint check before `stop_runtime_in_session`, launch_operations.py:127-128); W3RCB-08 = DOC-10 commerce `held` path (ownership_gate absent everywhere); W3CEA-16 / W3RD-28 / W3RD-10 legacy `catalog_terms` + vault_editing parts (catalogue_select / catalogue_terms parts absent); W3DC-01 urllib (session_bootstrap.py:37, :419, :529); W3FA-02 `FansApiClient.send` (protocol.py:246).
- G1: y 10 / n 2. y: W3CEA-07, W3RCB-08, W3RCB-06, W3FA-01, W3FA-04, W3FA-12, W3CEA-08, W3CEA-25, W3RD-05, W4OFR-05. n: W3CEA-09 (history_ownership.py absent), W4CEB-4 (ownership_gate.py absent).
- G2: y 10 / n 0. y: W3TSA-05, W3RCA-02, W3RCA-03, W3RD-06, W3RCA-01, W3RCA-S1, W4CEC-1, W4CEB-10, W4MT-3, W4MT-6.
- G4: y 7 / n 0. y: W3RCT-01, W3RCT-02, W3RCT-04, W3RCT-07, W3RCT-28, W3RD-02, W3CEA-16.
- G5: y 22 / n 2. y: W3TL-01, W3DR-07, W3TL-02, W3TL-04, W3TL-05, W3TL-06, W3TL-07, W3TL-08, W3TL-11, W3TL-09, W3DR-23, W3TL-10, W3DR-08, W3TL-14, W3TL-18, W3TL-23, W3TL-27, W3RCB-10, W3RO-14, W4MT-1, W4CEB-1, W4MA-05. n: W3DR-01 (R0-only gzip decode; openrouter.py has none), W3DR-02 (R0 release/handover path; base = F2 y).
- G6: y 14 / n 0. y: W3RD-01, W3RCB-14, W3RO-11, W3RO-10, W3RO-09, W3RO-12, W3RCA-08, W3RD-10, W3RD-28, W3RD-31, W3RD-32, W4CEB-7, W4MA-08, W4MB-06.
- G7: y 35 / n 0. y: W3RCB-01, W3RCB-04, W3RO-01, W3RCB-03, W3RO-02, W3RCB-02, W3RCT-14, W3TSA-12, W3RCT-10, W3RD-07, W3RCB-15, W3RCB-05, W3RCB-11, W3RCB-12, W3RCB-13, W3RCB-16, W3RO-03, W3RO-04, W3RO-05, W3RO-18, W3RCA-06, W3RCT-05, W3RCT-06, W3RCT-08, W3RCT-09, W3RCT-13, W3RD-03, W3RD-04, W3RD-11, W3RD-29, W3FA-08, W3FA-09, W4CEB-2, W4OFR-03, W4OFR-04.
- G8: y 19 / n 0. y: W3CEA-01, W3CEA-02, W3CEA-03, W3CEA-12, W3TSA-02, W3RO-06, W3RO-07, W3RCA-07, W3RCT-11, W3RCT-12, W3TSA-09, W4SS-3, W4SS-1, W4SS-8, W4CEB-5, W4CEB-9, W4CEC-2, W4CEC-3, W4MT-2.
- G9: y 11 / n 0. y: W3TSA-01, W3TSA-03, W3TSA-04, W3FA-02, W3FA-14, W3TSA-11, W3RCA-04, W3RCA-05, W3RCB-09, W3TSA-S3, W4CEB-3.
- G10: y 13 / n 0. y: W3FA-03, W3RO-36, W3FA-07, W3RCT-03, W3RCT-15, W3DC-03, W4TI-01, W4TI-02, W4TI-06, W4DM-06, W4MA-11, W4OFR-06, W4OFR-28.
- G11: y 12 / n 0. y: W3RCA-10, W3RCA-23, W3TL-03, W3TL-26, W3TL-28, W3TL-29, W3DC-01, W3RCT-26, W4MB-02, W4DM-05, W4DM-28, W4MA-10.
- G12: y 5 / n 0. y: W3RCB-07, W3RO-08, W3TL-22, W3TH-09, W4MB-01.
- G13: y 25 / n 0. y: W4DS-01, W4DS-02, W4MB-08, W4MB-13, W4DM-01, W4DM-02, W4DM-03, W4DM-04, W4MA-01, W4DM-07, W4DM-21, W4MA-02, W4MA-03, W4MA-04, W4MA-06, W4MB-07, W4MA-07, W4MA-09, W4MB-03, W4MB-04, W4MB-05, W4MB-12, W4MB-10, W4MB-09, W4MB-11.
- G14: y 3 / n 0. y: W4CEC-4, W4CEB-6, W4CEB-8.

## W4 disputes
- **OPEN: W4MA-05 vs W4DS-09** — `llm_client._reserve` with `account_id=None`? Owner W5 llm-profile-learning (G5).
- W4SS-7 severity (Sol blocker vs lane minor/doc) → policy row.
- W1-10 should be minor (W4SS, i). W1-12 partial: NULL due → wasted turn, no early wire call (W4MT, i).
- W1 DB-17 wrong: `of_vault_sync_frozen` blocks DELETE (W4DS m, Sol agrees; DS-2 trigger live m).
- W3TL-04 latent-dead (W4MT, Sol confirmed). W3FA-14 wording; W3FA-10 reach guard wrong (W4MT, i).
- G2 "659ea29: n" partly wrong: legacy text-only stop y (W4CEC, i).
- W3DR-01 fixed by 3b8fc64c (W4DD m); W1 DB-04 fixed at a2f1dda6 (W4CEB m); W3TL-23 closed as test-gap (W4CEB m).
- W4MB DA "live 0133 skipped 0129b" refuted (W1 C01 + W4 DS-1 / MB-3 m).
- Unplaced: none.

# W5+W6 (a2f1dda6 + deltas c48eb755 / 8d0001f9..bd47f63d, closed 2026-11-12)

- Source: `.scratch/rwl-W5/STATE.md` + `.scratch/rwl-W6/STATE.md` (Summary, Live-risk, Orphan), 25 lane `FINDINGS.md` (W5 11, W6 14), live `rwl/W6-live-verify:.scratch/rwl-W6/live-verify/RESULTS.md` (38 checks: 28 confirmed / 5 failed / 5 blocked, 0 OF calls). Programme `.scratch/rwl-programme/STATE.md`; Fable 17:4x decisions applied.
- Pins: lanes at a2f1dda6; delta lanes delta-browserbase-login (c48eb755), delta-history-reads (8d0001f9..bd47f63d). Running (m): engine `8d0001f9` (since 11:43:23); **OF worker restarted 15:34:07 on 943f3851 (unreviewed)**; creator sign-in listener :8479 up since 2026-11-11.
- Live (m, W6 S0): sending OFF — 0 delivery policies, 0 pilot scope, 0 automation rules, 0 deliveries, 0 model calls. Every harm below is latent unless marked **LIVE**.
- Same rules as W1–W4. Id prefixes: W5 BB ED LP ME MR NL NR NT RL TA TB; W6 HR SI OC VA VS AX CE MW AA C1 C2 C3 AD AE; `-m<n>` minor, `-S<n>` structure; `-L<n>` live checks not counted.

## W5+W6 counts

- raw: 1373 lane-local ids. Withdrawn, not counted: W5ED-26, W5ME-21, W5ME-24, W5NL-25, W5NR-19, W5NR-38, W5TB-19, W6HR-13, W6HR-18 → **1364** (W5 333, W6 1031).
- per lane: BB 13, ED 27, LP 41, ME 25, MR 34, NL 36, NR 49, NT 24, RL 27, TA 34, TB 23 · HR 17, SI 55, OC 72, VA 82, VS 90, AX 71, CE 99, MW 81, AA 63, C1 69, C2 82, C3 59, AD 89, AE 102.
- severity: blocker 33, blk-cond 32, major 479, minor 755, nit 55, unlabelled 10 (W6MW-26 … 35, structure).
- orphans (no lane id): W6-ORPH-1 (= W5NL-8), W6-ORPH-2, sell_lane.py:467 → G16.

| verdict | ids |
|---|---|
| picked (6 groups) | 251 |
| fix | 177 |
| YAGNI | 922 |
| policy — owner | 9 |
| dup | 5 |
| **total** | **1364** |

- defaults for the rest: blocker / blk-cond → fix; major → fix, unless structure (-S, class structure, W6MW-26 … 35) or test-gap → YAGNI; minor / nit → YAGNI.
- groups: G14 38 (blk 10, cond 7, maj 16, min 5) · G15 48 (blk 7, cond 11, maj 29, min 1) · G16 27 + 3 orphans (blk 1, maj 19, min 7) · G5 45 (blk 2, cond 2, maj 37, min 4) · G8 42 (blk 4, cond 9, maj 28, min 1) · G10+G11 51 (blk 4, cond 1, maj 45, min 1).

## W5+W6 groups

### G14 (extended) · Content safety — fix before ANY autonomous send
- root cause: no age / minor gate on input, model output, or vision; the one "restriction" layer disguises restricted words instead of blocking them; describers are told not to refuse.
- ids: **W6AA-1** = W6C1-59 — blocker (_common.py:2581-2641 `_RESTRICTED_WORDS` + `_double_first_vowel`: restricted minor/age words get a doubled vowel "to slip the filter", not blocked; 16 relay call sites + native ai_chatter.py:10391); **W6AA-2** (fan under-18 self-statement detected then dropped; W6AE-m13 dup); **W6AA-3** (_outbound.py:54-164 `_guard` / `finalize_draft` has no minor / age, tip-ask or customs check); W6AA-4 = W6C3-3 (operator banned-word filter runs after restriction → `\bteen\b` never matches; ai_chatter.py:10391 → :10521, autoreply.py:814 → :827); W6AA-5 = W6AE-29 (typo layer _common.py:1777-1891 rebuilds the restricted word); W6AA-6, W6AA-7, W6AA-m2, W6AA-m26 (red 7/7 `service/tests/test_w6_automations_engine_a_red.py`); **W6C2-1** (ai_chatter.py:7434-7446 `_DESC_OFF_LIMITS_RE` deliberately omits child / minor terms); W6C2-2, W6C2-3, W6C2-4, W6C2-22, W6C2-57; **W6VA-1** — blk-cond (vault_ai_api.py:2429-2433 "do not refuse"; refusal escalates to a bigger model; no apparent-age field); W6VS-1 (vault_scripts.py:908-937 story / mass lanes by model tier only); W6VS-76; W6CE-2 (inbound_describe.py:65-68, :194-215 "Do not refuse"; child = "awkward"); W6MW-1 — blocker (webhook_dispatch.py:875-965 inbound photo describe has no safety outcome); W6MW-21 (vision.py:66 `is_refusal` needs len < 40; stock refusal is exactly 40, m); **W6AE-1** — blocker (bot asks age, stores "16", keeps going; native via native_planning.py:105 → ai_chatter.py:6713); **W6AE-2** (wcfi.py:844-845, :984 fan fetish text into system prompt unchecked); **W6AE-4** (wcfi.py:2115-2177 no output check before the wire); W6AE-32; **W6AD-5** (pack_claim.py:70-92 `_DELIVERY_PROMISE` misses chat-spelled media promises, m); W6AD-15, W6AD-22, W6C3-25; **W5NR-5** — blk-cond (safety_detect.py:30-41, :233-255 no minor / age category; red `rwl/W5-native-pilot-runner:service/tests/test_w5_nr_safety_detect.py` 8 red m); W5NR-6, W5NR-7; W5MR-1, W5MR-14, W5TA-3.
- live: latent — sending off (m); W4 reach 187 matching inbound still applies (m count).
- fix: one `safety_gate(text)` (NFKC + apostrophe / zero-width normalise; minor / age / step-* / school terms; self-stated age < 18 in digits or words) run on fan input AND final output after every rewrite layer, before the wire; hit ⇒ hard stop + human handover. Delete `apply_word_restriction` disguise for minor / age words (block, never mutate). Describers: allow refusal, refusal ⇒ hold + no reuse, add `apparent_minor` output. Move gating tests top-level.
- OQ: **OQ-25** (default hard stop; blocker before any autonomous send).

### G15 (new, relates G6) · Money asks / offers: tips, customs, invented prices
- root cause: prompts and code-default ladders ask for tips, sell customs and compute prices per fan; no banned-kind gate; agency manual price not the only source.
- ids: **W6C1-1** — blocker (ai_chatter.py:518-548, :2652-2658 saved tip-ask flags run a tip-ask turn; `TEXT_ONLY` is a denylist); **W6C1-2** (ai_chatter.py:3123-3167 customs rule "He TIPS for it… SAY THE DOLLAR FIGURE"); **W6C1-3** (legacy tip offers render "only $N more"); W6C1-12, W6C1-15, W6C1-16, W6C1-31, W6C1-67, W6C1-69; **W6AE-5** — blk-cond (wcfi.py:1187-1198 → _customs.py:557-563 customs ETA = timed promise); **W6AE-6** (per-fan price from band / A-B arm / ceiling / cents tail); W6AE-7; **W6AE-8** (tip_reward_config.py:147-155 default rung prices $10-$200); **W6AE-11** (upsell.py:712-900 a sent card cut twice); W6AE-12, W6AE-13, W6AE-16; **W6AD-1** — blocker (pack price from fan spend ladder, not manual price); **W6AD-2** (pack_pricing.py:268-280 `MAX_SOFTEN_RUNGS` walk-down after refusal; red `service/tests/test_w6_automations_engine_d_red.py`); W6AD-3, W6AD-4, W6AD-9, W6AD-10, W6AD-12, W6AD-20; W6AA-10, W6AA-11; W6C2-8, W6C2-16, W6C2-17, W6C2-18, W6C2-23; W6C3-4, W6C3-6, W6C3-13, W6C3-20; W5MR-2, W5MR-3 (OQ-W5MR-a), W5MR-13, W5MR-17; W5NL-2; W5RL-4, W5RL-5 (red `test_w5_rl_persona_lint.py`); W6AX-1 — blk-cond (automation_registry.py:66-81 no banned-kind gate, incl. tip_request); W6VS-23, W6VS-24; W6OC-3 (of_client.py:3675-3680 `send_tip` guarded only by `_owner_fence`, no stop-chain); W6OC-70.
- live: latent — native vault-mode not running, relay not running, sending off (m).
- fix: banned kinds (tip ask, customs sell, timed promise) refused at `register()` and at the outbound gate; price = agency manual price or unsellable (no defaults, no ladder, no soften, no A/B); one discount per card, approved variant only; `send_tip` behind the stop chain.
- OQ: OQ-W5MR-a; with G6.

### G16 (new) · Send-path fail-open — **ARMED** the moment native sending is enabled
- root cause: `except Exception` around holds / fences returns "go"; pasted blocks reference undefined names, so the guard crashes into the fail-open branch.
- orphans: **W6-ORPH-1** = W5NL-8 (native_pilot/engine.py:464-467 `except Exception: return result  # fail open`; :496-502 undefined `result` → NameError swallowed → offer passes without repeat / unlinked-payment / already-bought checks; red `test_w5_nl_offer_blocker.py`); **W6-ORPH-2** (engine.py:509 `hero[offer_id]` KeyError on ordinary turns); **sell_lane.py:467** (`self.account_id` in module-level `qualify_snapshot` → NameError on the pull-lift path, escapes native_sales' except = crash, m, Sol confirmed).
- ids: W5NT-7 (attribution.py:513-538 manual-chatter yield fails open → `log.debug`); W5TA-5 (coordination.py:821-826 fence fail-closed depends on sender env; red `test_w5_ta_fence_fail_open.py`); W5TB-1 (worker.py:474-478, :509-513 lease + cooldown read as go on import error; red `test_w5_tb_fail_open_gates.py`); W5NR-10 (runner.py:504-507 hold read once before vault work), W5NR-13, W5NR-14, W5NR-17, W5NR-20, W5NR-22; W5MR-5, W5MR-6, W5MR-7, W5MR-19, W5MR-21; W6AE-19 (wcfi.py:1686-1695 ownership read error → bot owns); W6MW-6 (webhook_dispatch.py:398-405 spend gate → None, no log); W6MW-36 (webhook_dispatch.py:238 "false" string enables); W6AX-15 (audience_include.py:125-131 three fail-open paths), W6AX-m1; W6AD-m2, W6AD-m35; W6C1-9 (ai_chatter.py:1263-1276 corrupt config → full defaults); W6VA-48 (vault_ai_api.py:2205-2211); W6CE-m70; **W5ED-7** — blocker (gated_worker.py:19-45 pre-send stop exists only in an unlaunched ops monkeypatch); W5ED-23.
- live: latent — sending off (m); armed on first enable (i).
- fix: every guard read is fail-closed (exception ⇒ hold + log error), no `return result` in except; lint/test that every name in engine.py / sell_lane.py resolves (pyflakes in run-tests); move the pre-send stop into the real send path.

### G5 (extended) · Retry after an irreversible step (paid model call or OF POST)
- root cause: retry / requeue decided without knowing whether the paid call or the POST happened; reservation `unknown` treated as "retry".
- ids — paid regeneration: **W5LP-1** — blocker (profile learner re-calls after unknown; red `test_w5_lp_llm_client_spend.py`); **W5NR-3** — blocker (model.py:263-273 settle fail → draft discarded, input requeued, paid again); W5NR-4, W5NR-11, W5NR-12, W5NL-15; W6AE-22, W6AE-23, W6C3-31, W6C2-13; W6VA-3, W6VA-4, W6VA-5, W6VA-6; W6CE-17 = W6MW-2 (webhook_dispatch.py:92-100 describe budget counts only successes); W5LP-2, W5LP-3, W5LP-4, W5LP-7, W5LP-8, W5LP-9, W5LP-40; W5ME-4, W5ME-5, W5NT-4, W5MR-4, W5MR-16, W5NR-33, W6AE-40, W6C3-11.
- ids — duplicate sends: **W6AX-2** — blk-cond (automation_executor.py:1756-1778 → :666-676 failed broadcast requeued → up to 3× same mass send; red `test_w6_auth_executor_red.py`); W6AX-5 (:1797-1801 orphan reconcile, no cap); W6AX-23; W6AE-24 (wcfi.py:2192-2213 timeout after OF accepted = "nothing sent"); W6C3-21; W6AD-6 (tip_reward.py:198-240 check → wire → record); W5NL-10; W5TA-4 (red `test_w5_ta_prewire_unknown.py`), W5TA-7; W5NR-16; W5TB-16; W6CE-15; W6MW-4; W6OC-24.
- live: latent — 0 model calls, sending off (m).
- fix: G5 fix + record-before-wire (intent row, then POST, then settle); unknown POST ⇒ reconcile against OF history, never resend; unknown model call ⇒ hold, never re-call; executor: failed job after `fn()` started ⇒ `unknown`, not `pending`.

### G8 (extended) · Newer input / staff / stop not honoured
- root cause: send decisions read input / staff / stop state once, early; skipped rows never advance the input head.
- ids: **W6HR-1** — blk-cond, **LIVE code** (native_connector.py:204-220, store.py:878-895: skipped tip / unsent / deleted row never `_record`ed → newest inbound never bumps input; runs on the OF worker at **943f3851, unreviewed** (i via W6HR-2 m HEAD); 0 rows_skipped counted (m) → harm latent while sending off); W6HR-2 (recent_inbox.py:95-97 head stuck on a skipped row), W6HR-3 (recent_inbox.py:103-106; vacuous live), W6HR-4; **W6MW-78 — LIVE read path** (unread_preservation.py:78-86 snapshot → fan sends → `read()` clears new badge, i/g); **W6MW-77 — live-checked** (unread_preservation.py:41-42 hard-coded journal dir; L8-1 i: worker env sets ONLYFANS_UNREAD_JOURNAL_DIR, default dir absent; L8-2 0 errors m → latent); W5NL-1, W5NR-1 — blk-cond (service.py:1284-1291 `tolerate_newer` after 45 s), W5NR-2 (OQ-29a), W5NR-8, W5NR-9 (OQ-18), W5NR-15; W5TA-1, W5TA-2, W5TA-6; W5TB-4, W5TB-5; W5MR-8, W5MR-12; **W6C3-1** — blocker (ai_chatter.py:7977-7988 payload jobs skip `skip_list`), W6C3-22; W6C1-4 (ai_chatter.py:3833-3869 untagged staff reply in the OF app not a stand-down), W6C1-5, W6C1-6, W6C1-7, W6C1-10, W6C1-17; W6C2-5, W6C2-11, W6C2-19, W6C2-20; W6AE-3, W6AE-9, W6AE-10, W6AE-36, W6AE-39; W6AD-7, W6AD-17; W6MW-5; W6AA-8; W6AX-4; W5NL-16.
- live: W6HR-1 / W6MW-78 code is running (i); harm latent — sending off (m).
- fix: G8 fix + skipped rows still advance the input head (record a placeholder); re-read newest inbound + staff + stop at the wire for every job kind incl. payload jobs; unread restore by message id, not badge; journal dir from VAULT_SPOOL_DIR.
- OQ: OQ-18, OQ-29a. Unowned: recent_inbox path → W7 delta.

### G10+G11 (extended) · Cross-account scoping + credentials / exposure
- root cause: login / sign-in / SSE / key lookup trust caller-supplied ids; no principal check; third-party custody of sessions.
- ids — LIVE: **W5BB-1..4** (browserbase_login.py:284-289, :328-336 returned `ownerId` never compared with the account; :440 shared proxy pool; :435-443 session recording defaults; :247-250 cancel w/o release) — **Browserbase key still set** (carry-a m); login ran 6× since 11:43, bindings 1 → 1 → no cross-account capture so far (m); wrong-creator save needs a first link of a new account (i). **W6SI-18** (creator-sign-in/server.mjs:44 binds 0.0.0.0) — **listening on *:8479** (W6SI-L1 m); unauthenticated → 401 (W6SI-L2 m). **W5ME-9** (9a: `onlyfans_proxy.inventory` absent ⇒ one-proxy-per-account fence inert, W5 L12 m). **W6MW-17** = W5LP-5 = W6VA-17 (tenant_keys.py:90-135 `owners_of` `[]` ⇒ house key billed) — L8-3 m: 1 account, 0 user_accounts links; 0 model calls → harm latent.
- ids — other: W6SI-1 (creator-sign-in/server.mjs:9, service.mjs:76 reconnect skips the network broker; proxy + owner from caller), W6SI-2, W6SI-5, W6SI-6, W6SI-7, W6SI-8, W6SI-9, W6SI-10, W6SI-13, W6SI-14; **W6CE-1** — blocker (events.py:95-108, :209-212 SSE no principal scoping; relay-only), W6CE-3, W6CE-4, W6CE-6 … 13; W6AX-7 (auth.py:817, :938-944 `require_master` unused → any signed-in user lists / deletes users), W6AX-8 … 12, W6AX-14, W6AX-27; W5ME-2, W5ME-3, W5ME-6, W5ME-26; W6MW-15, W6MW-16 (secrets_store.py:230-247 unreadable secrets cached `{}`); W6OC-6; W5RL-1, W5RL-8; W5MR-9; W6C3-24; W5LP-6, W5LP-10.
- live: see LIVE ids; rest latent (relay not running / 1 account).
- fix (owner first): unset the Browserbase key until `ownerId` == account check lands; bind :8479 to 127.0.0.1 / compose network; create `onlyfans_proxy.inventory` or make absence fail-closed; `owners_of` empty ⇒ refuse paid call, never house key; SSE + auth routes require principal / master.
- OQ: OQ-26, OQ-28.

## W5+W6 rest

- notable rest fixes (blockers / blk-cond): W5ED-2 (Dockerfile.onlyfans:7 × .dockerignore misses gitignored secrets, G11 class); W5NT-1 (load_test.py:318-321 can wipe the engine DB — data loss, manual tool); W6OC-1 (of_client.py:2164-2204 OF-native welcome, no owner fence, G1/G9 class); W6VA-2 (vault_ai_brief.py:388-422 sellability ignores confidence, G4 class); W6CE-5 (employees.py:749-761 Automation sentinel spoofable → attribution).
- class hints (fix with the named W3/W4 group): G1 W5NL-4, W6OC-8 … 12, W6C1-8, W6C1-13, W6C1-68, W6C2-14, W6AA-12 · G4 W5NL-3, W5MR-18, W6AA-13, W6C1-14, W6AD-11, W6AD-18, W6VS-5, W6VA-18, W6VS-90 · G7 W5RL-2, W6OC-15, W6AX-17, W6AX-21, W6VS-13 … 18, W5ME-1 · G9 W6VS-12 (vault_upload.py:889-983 uploads = OF writes, no stop check; engine-reachable i; W6VS-L1 0 m), W6OC-2, W6OC-4, W6AX-3.
- policy — owner (9, values never copied, OQ-15): W6AD-16, W6MW-9, W6OC-72 (public-repo scrubs), W5ED-1, W5NT-2, W5MR-15, W6C1-64, W6C2-61, W6AE-m4.
- dup (5): W6AE-m13 = W6AA-2; W5ED-16 = W3RCT-14; W6AX-28 = W1 DOC-08; W6VA-81 = CVA-16; W6AE-m10 = W6VS-24.

| lane | fix | YAGNI | dup / policy |
|---|---|---|---|
| W5BB (delta-browserbase-login) | — | W5BB-5 … 13 (9) | — |
| W5ED (engine-deploy) | W5ED-2 … 6, W5ED-8, W5ED-9, W5ED-28 | W5ED-10 … 15, W5ED-17 … 22, W5ED-24, W5ED-25, W5ED-27 (15) | dup: W5ED-16; policy: W5ED-1 |
| W5LP (llm-profile-learning) | — | W5LP-11 … 39, W5LP-41 (30) | — |
| W5ME (moxy-eval-proxy) | W5ME-1 | W5ME-7, W5ME-8, W5ME-10 … 20, W5ME-22, W5ME-23, W5ME-25, W5ME-27 (17) | — |
| W5MR (moxy-runtime) | W5MR-10, W5MR-18, W5MR-20, W5MR-24 | W5MR-11, W5MR-22, W5MR-23, W5MR-25 … 34 (13) | policy: W5MR-15 |
| W5NL (native-pilot-ledger) | W5NL-3 … 7, W5NL-14, W5NL-26 | W5NL-9, W5NL-11 … 13, W5NL-17 … 24, W5NL-S1 … S11 (23) | — |
| W5NR (native-pilot-runner) | W5NR-18, W5NR-23 … 25 | W5NR-21, W5NR-26 … 32, W5NR-34 … 37, W5NR-39, W5NR-S1 … S12 (25) | — |
| W5NT (native-pilot-tools) | W5NT-1, W5NT-3, W5NT-5, W5NT-6, W5NT-15 | W5NT-8 … 14, W5NT-16 … 24 (16) | policy: W5NT-2 |
| W5RL (runtime-learning) | W5RL-2, W5RL-3, W5RL-6, W5RL-7 | W5RL-9 … 17, W5RL-S1 … S10 (19) | — |
| W5TA (twin-bridge-a) | W5TA-8 … 10 | W5TA-11 … 34 (24) | — |
| W5TB (twin-bridge-b) | W5TB-2, W5TB-3 | W5TB-6 … 15, W5TB-17, W5TB-18, W5TB-20 … 24 (17) | — |
| W6HR (delta-history-reads) | W6HR-5 | W6HR-6 … 12, W6HR-14 … 17, W6HR-19 (12) | — |
| W6SI (extensions-sign-in) | W6SI-4, W6SI-11 | W6SI-3, W6SI-12, W6SI-15 … 17, W6SI-19 … 55 (42) | — |
| W6OC (of-client) | W6OC-1, W6OC-2, W6OC-4, W6OC-7 … 12, W6OC-14 … 16, W6OC-71 | W6OC-5, W6OC-13, W6OC-17 … 23, W6OC-25 … 69 (54) | policy: W6OC-72 |
| W6VA (vault-ai-api) | W6VA-2, W6VA-7 … 16, W6VA-18 … 20 | W6VA-21 … 47, W6VA-49 … 80, W6VA-82 (60) | dup: W6VA-81 |
| W6VS (vault-scripts) | W6VS-2 … 7, W6VS-10, W6VS-12 … 18, W6VS-26, W6VS-29 … 32, W6VS-34 … 37, W6VS-54, W6VS-89, W6VS-90 | W6VS-8, W6VS-9, W6VS-11, W6VS-19 … 22, W6VS-25, W6VS-27, W6VS-28, W6VS-33, W6VS-38 … 53, W6VS-55 … 75, W6VS-77 … 88 (60) | — |
| W6AX (auth-executor) | W6AX-3, W6AX-6, W6AX-13, W6AX-16 … 22, W6AX-24 … 26, W6AX-35, W6AX-36 | W6AX-29 … 34, W6AX-m2 … m35 (40) | dup: W6AX-28 |
| W6CE (chatters-employees) | W6CE-5, W6CE-14, W6CE-16, W6CE-18 … 21 | W6CE-22 … 26, W6CE-m1 … m69, W6CE-m71 … m73 (77) | — |
| W6MW (media-webhook-misc) | W6MW-3, W6MW-7, W6MW-8, W6MW-10 … 14, W6MW-18 … 20 | W6MW-22 … 35, W6MW-37 … 76, W6MW-79 … 81 (57) | policy: W6MW-9 |
| W6AA (automations-engine-a) | W6AA-9, W6AA-12 … 14, W6AA-m44 | W6AA-15 … 19, W6AA-m1, W6AA-m3 … m25, W6AA-m27 … m43 (46) | — |
| W6C1 (ai-chatter-1) | W6C1-8, W6C1-11, W6C1-13, W6C1-14, W6C1-68 | W6C1-18 … 30, W6C1-32 … 58, W6C1-60 … 63, W6C1-65, W6C1-66 (46) | policy: W6C1-64 |
| W6C2 (ai-chatter-2) | W6C2-6, W6C2-7, W6C2-12, W6C2-14, W6C2-21, W6C2-24, W6C2-25, W6C2-81 | W6C2-9, W6C2-10, W6C2-15, W6C2-26 … 56, W6C2-58 … 60, W6C2-62 … 80, W6C2-82 (57) | policy: W6C2-61 |
| W6C3 (ai-chatter-3) | W6C3-2, W6C3-5, W6C3-8 … 10, W6C3-12, W6C3-15, W6C3-16, W6C3-18, W6C3-19, W6C3-23 | W6C3-7, W6C3-14, W6C3-17, W6C3-26 … 30, W6C3-32 … 59 (36) | — |
| W6AD (automations-engine-d) | W6AD-8, W6AD-11, W6AD-14, W6AD-18, W6AD-19 | W6AD-13, W6AD-21, W6AD-23 … 27, W6AD-m1, W6AD-m3 … m34, W6AD-m36 … m61, W6AD-n1 (67) | policy: W6AD-16 |
| W6AE (automations-engine-e) | W6AE-14, W6AE-15, W6AE-17, W6AE-20, W6AE-21, W6AE-25 … 28, W6AE-30, W6AE-31, W6AE-33 … 35, W6AE-37, W6AE-38 | W6AE-18, W6AE-41 … 45, W6AE-m1 … m3, W6AE-m5 … m9, W6AE-m11, W6AE-m12, W6AE-m14 … m57 (60) | dup: W6AE-m10, W6AE-m13; policy: W6AE-m4 |

## W5+W6 disputes / open
- **RESOLVED: W4MA-05 vs W4DS-09** (W5 L3): no `account_id=None` caller; defect real, latent.
- live-verify blocked: ai-chatter-1-2, ai-chatter-1-3, ai-chatter-2-1, W6VA-L1, of-client-3. Failed, not a finding: chatters-employees-2 (employees table empty → W4MA-15 condition).
- unreviewed code running: OF worker 943f3851 (W6HR-1 path); commercial-integration-4 48e007a5 never reviewed; recent_inbox unowned → W7 delta.
- sub-references without rows (cited inside parents): W5BB-8c, W5ME-9a/9b, W5NR-41.
- Unplaced: none. W6MW-26 … 35 (no severity, structure) → YAGNI.
