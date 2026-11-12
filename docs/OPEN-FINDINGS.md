# Open-findings triage — W1+W2 (2026-11-12)

Review-only windows; nothing fixed by us. Team owns fixes; this file ranks them. IDs/counts only (public repo).

- Source: `.scratch/rwl-W1/STATE.md`, `.scratch/rwl-W2/STATE.md` (Threads, Summary, Orphan), `.scratch/rwl-programme/STATE.md` (Live-risk, OQ-1..9, Severity policy, Disputes), `git show rwl/W<n>-<slug>:docs/review/W<n>-<slug>/FINDINGS.md` for 16 review lanes, live results `.scratch/rwl-W{1,2}/live-verify/RESULTS.md`.
- Pins: W1 = 659ea29..e8e90954, W2 = e8e90954..a2f1dda6. Live trial commit = 659ea29.
- Live stack (measured, both live-verify lanes, 0 OF calls): DB at `0133`; pin head `0136` → every finding on 0134–0136 objects is latent on the running stack. Sending disabled: 0 delivery policies, runtime off, 0 scope / intent / delivery / grant rows, 0 automations, tip_request off. 0 vault / catalogue / evidence / model-call rows → most sellability + ownership checks vacuous (OQ-6).
- Rule: severity = harm if reached (written rule); Reach = live|latent + the one guard keeping it latent + m/i/g (programme Severity policy). Latent blocker gates lifting its guard, not a merge.
- Dedupe: by file:line across lanes/windows; W2 "W1 carry-over" rows are restatements (not recounted). Cross-lane dups noted `=`.
- Picks: bug/data-risk first (send twice / wrong fan, cross-tenant, money/ledger/ownership, data loss, race, 5xx); ≤6 groups. Everything else one word.

## Counts

- raw: 404 lane-local ids (W1 222 = L1 35, L2 11, L3 36, L4 26, L5 26, L6 16, L7 29, L8 43; W2 182 = db 19, scope 22, sale 36, runtime 25, fans-api 27, tests-commercial 21, tests-history 21, claims-sim 11). Measured by grep of FINDINGS ids; W2 sale focus labels F1–F8 excluded.
- picked: 92 ids in 6 groups. rest: 312 (lane-local ids; ` = ` dups inside groups still counted per id).
- live trial (659ea29) present: G1 y (narrowed), G4 y (native case), G5 y (W1-04, F6); G2/G3/G6 n. Rest: S1 (R11/R13) y m, W2SC-14 y m.
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
- 659ea29: y (DOC-10/CS-02 mechanism m, narrowed; residual i); W2-new parts n.
- fix: no evidence / no import / malformed JSON / non-message purchase / dismissed money row ⇒ ownership `unknown` ⇒ refuse paid card; scope history lookups by fan + binding.
- red: `rwl/W2-conversation-engine-sale:service/tests/test_w2_conversation_engine_sale_pg.py` (w2s02, w2s03, w2s04b, w2s07, w2s11); `rwl/W2-fans-api-adapter:service/tests/test_w2_fans_api_adapter_pg.py` (w2fa_01 ×2, w2fa_03). DOC-10 malformed-JSON half: idea only.
- OQ: OQ-8 (import freeze → W2FA-17); W2FA-02/07 conditional defaults "no" (lane, not numbered).

### G2 · Emergency stop refusable; dashboard activate widens commercial scope (send after stop)
- ids: W2SC-02, W2SC-11, W2SC-22 = W2OR-04, W2OR-18, W2OR-19, W2OR-20, W2OR-08, W2OR-09, W2OR-10; F03 (W1 L4, connections.py:332-344).
- what: stop/pause re-validates scope + limits inside `_configure_in_session(scope_stop=True)` → `Refused` rolls back runtime-off + queue cancel; activate sets native + media + 10000 units + runtime on with no scope check, admission admits any fan; then stop is refused.
- severity: blocker (inv. 4 "stop never refusable"). Reach: latent — guard = no commercial scope can exist live (DB 0133 < 0134, 0 scope rows; m). After 0134 the only guard is nobody pressing dashboard activate (m by reading).
- 659ea29: n (stop was a pure runtime write, m; activate code unchanged since 659ea29 but needs a scope to harm, m).
- fix: one validation-free `stop_scope` (runtime off, queue cancelled, policy off, scope stopped, no config read); activate calls `assert_enable_allowed`, refuses on commercial/preparation scope.
- red: `rwl/W2-conversation-engine-scope:service/tests/test_w2_scope_commercial_route_pg.py` (emergency_stop ×2); `rwl/W2-onlyfans-runtime:service/tests/test_w2_onlyfans_runtime_pg.py` (w2or04, w2or18).
- OQ: OQ-7 (non-audience sends on active scope; default hold).

### G3 · Terms-only commercial route mints standing grant without promotion (wrong fan / unaudited sale)
- ids: W2SC-01, W2SC-03, W2SC-04, W2S-01, W2S-10, W2S-27, W2S-30, CS-03, W2DB-01, W2DB-04 (terms row can price another account's item/binding, inv. 8), W2DB-05, W2DB-08.
- what: any approved `of_catalogue_terms` row makes an unpromoted account "commercial"; `prepare_commercial_offer` mints from caller `coverage_ref`/`valid_until` without #4 holds or audience; damaged scope row = "no pilot" opens holds; promotion FK accepts any launch command; lineage NULL passes.
- severity: blocker. Reach: latent — guard = `of_catalogue_terms` absent live (0135 not applied, m) + `prepare_commercial_offer` has no non-test caller (m).
- 659ea29: n (m: whole route added in W2).
- fix: one commercial predicate = validated, promoted, active scope row with explicit audience; terms alone never commercial; grant coverage from validated config + `prepare_pilot_offer` hold block; FK terms → same-account item + binding.
- red: `rwl/W2-conversation-engine-scope:service/tests/test_w2_scope_commercial_route_pg.py` (terms_alone, never_mints, damaged ×2); `rwl/W2-conversation-engine-sale:service/tests/test_w2_conversation_engine_sale_pg.py` (w2s01a ×2, w2s01b); `rwl/W2-db-migrations:service/tests/test_w2_db_migrations_pg.py` (w2db01, w2db04 ×2, w2db05).
- OQ: OQ-7.

### G4 · Unsellable media offered (not-ready / error / DRM / vanished / uncertain)
- ids: F01 (W1 L4), N1 (W1 L4 R2), L3-01, L3-02, L3-03, CVA-01, T01, T12, DOC-01, DOC-02, DOC-13, W2S-05, TC-07.
- what: `catalog_terms` checks only `removed_at` + kind; sync un-removes retained not-ready rows; DRM skip deleted; vanished never retired; uncertain items keep earlier category membership; new W2 shortlist/price path adds no DRM/vanished gate; tests pin the open gap as correct (T01).
- severity: blocker (CVA-01, DOC-02 conditional on OQ-4; default per-asset → blocker). Reach: latent — guard live = sending disabled + 0 vault/catalogue rows (m); in code: preparation-scope hold @e8e (m), lifted for an active commercial scope @a2f (i).
- 659ea29: y for native not-ready/vanished (F01 R2, m); FansAPI / DRM / uncertain parts n.
- fix: one per-asset readiness gate (ready, canView, !hasError, !DRM, in window, classification certain) inside `catalog_terms` / offer prepare for every paid path.
- red: `rwl/W1-fans-api-adapter:service/tests/test_w1_fans_api_adapter_pg.py` (l3_01_02 ×4, l3_01b, l3_03); `rwl/W2-conversation-engine-sale:service/tests/test_w2_conversation_engine_sale_drm_pg.py`; `rwl/W1-commands-vault-ai:service/tests/test_w1_commands_vault_ai_classification.py` (CVA-01); `jaka/rules-tests-20261111:service/tests/test_rule_retained_vault_unsellable_pg.py` (X01).
- OQ: OQ-1, OQ-2, OQ-3 (ask owner first), OQ-4, OQ-6.

### G5 · Paid-call ledger: unknown model call re-called; paid FansAPI reads re-bought
- ids: W1-04, F6 (both pre-existing), W1-01, W1-02, W1-05 = F2, W1-06, W1-32, F1, F5, L3-05, L3-08 ≈ W2FA-06, L3-09, W2SC-05, W2SC-07, W2SC-08, W2FA-05, W2FA-13, W2FA-14, W2FA-19, DOC-07, DOC-17.
- what: runtime retries a ledger `unknown` row (same-pass fallback / replay) → second possibly-charged call; Retry-After `'²'` turns refusal into held `unknown` (no human); local/unclassified preparation failures non-terminal → identity GET re-bought every pass; Retry-After clamped to 3600 / per-fan only → paid 429 storm; caption path outside ledger.
- severity: blocker (W1-01) / major (rest). Reach: latent — W1-04 guard = scheduler `uncertain` filter + fallback off by default (i; webhook redelivery path bypasses filter, open); live 0 model-call rows (m); paid reads: preparation unconfigured live (0 scope rows, m), bounded by scope budget.
- 659ea29: y (W1-04 line identical, m; F6 narrowed, m); rest n.
- fix: `unknown` row ⇒ hold, never re-call; refusal never → `unknown`; local/unclassified failures terminal or counted; honour provider Retry-After per account, no 3600 clamp.
- red: `rwl/W1-model-transport:service/tests/test_w1_model_transport_pg.py` (f1, f2, f5); `rwl/W2-conversation-engine-scope:service/tests/test_w2_scope_history_pg.py` (W2SC-05, W2SC-06); `rwl/W2-fans-api-adapter:service/tests/test_w2_fans_api_adapter_pg.py` (w2fa_05, w2fa_06). W1-04 / W1-01: none (needs PG; lane skipped).
- OQ: none numbered (W1-13 402 handover, unnumbered).

### G6 · Wrong price frozen on a card (money)
- ids: F02 = CVA-11 → W2OR-01 (worse), W2OR-02, W2OR-11, W2OR-12, W2S-08, W2S-09, CVA-08.
- what: `approvedTotalCents` accepted on a single (preview counted as bundle) and frozen as `agency_category` price; category price change never reaches approved singles; keyword `asks_cheaper` grants discounts on non-asks; discount grant replaced by standard successor at higher price; price authz tenant-wide with self-asserted actor.
- severity: major. Reach: latent — guard live = drafts created disabled + 0 vault rows + sending disabled (m); W2OR-01/02 need 0135 (absent, m).
- 659ea29: n (m: in-window code).
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
| CVA-04, CVA-06, CVA-10, CVA-12, CVA-13, CVA-14, CVA-15, CVA-16, CVA-18 | W1 L5 | YAGNI | CVA-10 dispute open (V05 blocked) → W3 |
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
