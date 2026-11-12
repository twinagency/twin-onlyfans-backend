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
| W3DR-06 | policy | OQ-21: R0 cannot boot on live DB 0133 (X01 m) |
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
