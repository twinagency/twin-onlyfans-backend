# Backend fix-queue triage — head `91e32acc`

- Head: `origin/claude/commercial-r0-r3-20261111` = `91e32acc181f079294b4370b660edd32e8ff4500` (m, `git rev-parse`).
- Since `a253c84c`: #84 `b9a8c516`, #85 `6ca2172a`, #86 `bdc6ad2b`, #87 `9124b50d`, #88 `96609e23`, #89 `91e32acc` (m).
- Tests: all 52 sweep files from `22fbbe79`, serial, throwaway PG :55650, network proxied to :9; 600 s/file. Two tracked test files (`test_w3_delta_curl_80_capture.py`, `test_w5_bb_login_review.py`) were replaced by their newer head versions from #84–#87 and rerun (m). `test_w5_ed` repro run from `e7c23c86` (m).
- `m` = measured here; `i` = inferred from queue/OPEN-FINDINGS, not rerun; `g` = guess.

## Counts (findings / groups)

| bucket | findings | groups |
|---|---:|---:|
| blk | 25 | 12 |
| sec-money maj | 14 | 11 |
| maj | 27 | 16 |
| minor | 3 | 1 |
| relay-deferred | 13 | 4 |
| fixed-by-team | 0 | 0 |
| fixed-by-us | 0 | 0 |
| closed | 10 | 0 |
| in-flight | 2 | 0 |
| drift-unknown | 25 | 0 |
| YAGNI-n/a | 4 | 0 |
| **total** | **123** | **44** |

- Open queued findings: 82 (94 queue ids − 1 duplicate W3RCB-03=W3RO-02 − 6 closed − 2 in-flight − 3 YAGNI).
- Green merges are booked under **closed**, per the explicit closed list; hence fixed-by-team/fixed-by-us are 0. `W3RCB-01` is partially green (2/5 cases, #83) and stays open under G04 (m).
- A further 22 live-risk W5/W6 ids have no red test and are listed under “Untested follow-ups”, not counted above.

## Groups (priority order)

`disjoint-with` = groups sharing no product file; parallel-safe. Files are product files, not tests.

| group | findings | bucket | product file:line | red test(s) at head | LOC | disjoint-with |
|---|---|---|---|---|---|---|
| G01 | W2S-02, W3FA-03 | blk (LIVE cross-tenant) | `service/ownership.py:179-185`; `service/fans_api_adapter/*` `_resolve` | `test_w2_conversation_engine_sale_pg::w2s02` RD (defect i); `test_w4_trace_isolation_resolve_pg` 2 RA (m) | ~100 | all |
| G02 | W2SC-02 | blk (LIVE stop) | `service/pilot_scope.py:703-715` | `test_w2_scope_commercial_route_pg::emergency_stop` RD (defect i) | ~60 | all except G10 |
| G03 | W3RO-01, W3RCB-03 | blk (LIVE reads) | `service/onlyfans_runtime/recent_inbox.py:47-49,95-97,187-198` | `test_w3_ro_recent_inbox_pg` 2 RA; `test_w3_rcb_recent_inbox_retry_pg` 1 RA (m) | ~100 | all |
| G04 | W3RCB-01 rest | blk (LIVE reads) | `service/onlyfans_runtime/native_protocol.py` `history_text` | `test_w3_rcb_history_page` 3 RA, 3 G (m) | ~80 | all |
| G05 | W5NR-5, W5NR-6, W4CEC-4, W6MW-21 | blk (ARMED content safety) | `service/native_pilot/safety_detect.py:30-41,64-66,161-255`; `service/conversation_engine/safety_context.py:8-38`; `service/vision.py:66` | `test_w5_nr_safety_detect` 8 RA + 3 RA; `test_w4_conversation_engine_c_safety::w4cec4` 7 RA; `test_w6_media_webhook_misc_red::test_w6mw_21` RA (m) | ~250 | all |
| G06 | L3-01, L3-02, L3-01b, L3-03, W2S-05 | blk (latent) | `service/fans_api_adapter/vault_sync.py:270-312`; `service/onlyfans_runtime/vault_management.py:285-315`; `catalogue_select.py:56-67` (absent) | `test_w1_fans_api_adapter_pg` 10 RA; `test_w2_conversation_engine_sale_drm_pg` RD (m) | ~180 | all except G14, G24, G26 |
| G07 | CVA-01 | blk (latent) | `service/vault_auto_folders.py:119-133` | `test_w1_commands_vault_ai_classification::test_uncertain...` RA (m) | ~40 | all except G25 |
| G08 | W3TL-01, W3TL-02 | blk (latent money) | `service/conversation_engine/model_runtime.py` replay/fallback; `model_transport/pilot_supervised.py` | `test_w3_trace_ledger_pg` 2 RA (m) | ~120 | all except G09, G13, G15, G32 |
| G09 | F3 | blk (secret persistence) | `service/model_transport/openrouter.py:57-58`; `service/conversation_engine/model_runtime.py:288` | `test_w1_model_transport_pg` 4 RA (m) | ~80 | all except G08, G13 |
| G10 | W3TSA-02, W4SS-1 | blk (latent stop) | `service/runtime_stop.py` `STRONGER`; `service/approval.py:110-122`; `service/pilot_scope.py:373-374`; `service/automations/_common.py:378` | `test_w3_tsa_ladder_stop` 3 RA; `test_w4_trace_supersede_seams_pg::w4ss1` RA (m) | ~150 | all except G02, G41 |
| G11 | W3CEA-07 | blk (latent) | `service/onlyfans_runtime/commerce.py:65-70` | `test_w3_cea_pending_evidence_pg` RD ImportError `history_support`; defect m at head | ~40 | all |
| G12 | W3TL-26, W3TL-07, W4MA-05 | blk | `service/llm_client.py:1121-1169,1133-1151` `_reserve` NULL | `test_w5_lp_llm_client_spend` 3 RA (m) | ~150 | all |
| G13 | F1, F2, F5 | sec-money maj | `service/model_transport/openrouter.py:36-48,74-99`; `model_runtime.py:221-230`; `pilot_supervised.py:375-382` | `test_w1_model_transport_pg` F1/F2/F5 RA (m) | ~150 | all except G08, G09, G15, G32 |
| G14 | F02, W2OR-01 | sec-money maj | `service/onlyfans_runtime/vault_management.py:485-491` | `test_w1_onlyfans_runtime_category_single_price` 2 RA; `test_w2_onlyfans_runtime_pg::w2or01` RD (m) | ~80 | all except G06, G26 |
| G15 | W3DR-02 | sec-money maj | `service/conversation_engine/model_ledger.py` release → `pilot_supervised.py` handover | `test_w3_delta_r0_live_pg::w3dr02` RA (m) | ~80 | all except G08, G13, G16, G32 |
| G16 | W4CEB-1 | sec-money maj | `service/conversation_engine/model_ledger.py:266,278-286,336-343` | `test_w4_ceb_lease_lapse_settle_pg` RA (m) | ~80 | all except G15 |
| G17 | W5ME-1 | sec-money maj | `service/managed_proxy/reservation.py:20` | `test_w5_me_legacy_reconnect_pg` RA (m) | ~50 | all |
| G18 | W6AX-7 | sec-money maj | `service/auth.py:817,938-944,1135-1148` | `test_w6_auth_executor_red::test_w6ax7_*` 2 RA (m) | ~50 | all |
| G19 | W6MW-16 | sec-money maj | `service/secrets_store.py:230-247` | `test_w6_media_webhook_misc_red::test_w6mw_16` RA (m) | ~60 | all |
| G20 | W6OC-14 | sec-money maj | `service/of_signer.py:93-125` | `test_w6_of_client_red::test_w6oc14` RA (m) | ~80 | all |
| G21 | W3DC-02 | sec-money maj | `service/session_bootstrap.py:554-564` | no head test; ad-hoc probe: wrong `staticParamOverride` accepted (m RA) | ~40 | all |
| G22 | W5TA-4 | sec-money maj | `service/twin_bridge/platform.py:336-350` | `test_w5_ta_prewire_unknown.py` 2 FAIL (m) | ~60 | all |
| G23 | W4OFR-07 | sec-money maj | `service/onlyfans_read/earnings.py:66-74` | `test_w4_onlyfans_read_earnings_pg` RA (m) | ~50 | all |
| G24 | L3-05, L3-06, L3-07, L3-08 | maj | `service/fans_api_adapter/vault_sync.py:172-256`; `protocol.py:66,77` | `test_w1_fans_api_adapter_pg` 4 RA (m) | ~150 | all except G06 |
| G25 | CVA-05 | maj | `service/vault_auto_folders.py:144-145` | `test_w1_commands_vault_ai_classification::test_operator_removed...` RA (m) | ~40 | all except G07 |
| G26 | F04 | maj | `service/onlyfans_runtime/vault_management.py:278-282` | `test_w1_onlyfans_runtime_mixed_family_ready` RA (m) | ~50 | all except G06, G14 |
| G27 | T17 | maj | `scripts/run-service-tests.py:111,116` | `test_w1_tests_runner_collection` 2 RA (m) | ~60 | all |
| G28 | W2OR-03 | maj | `service/onlyfans_runtime/text_delivery.py:218-229,635-641` | `test_w2_onlyfans_runtime_pg::w2or03` RD (manual half i) | ~80 | all |
| G29 | W3CEA-01, W3CEA-04, W3CEA-05 | maj | `service/conversation_engine/controls.py:55-100`; `claims.py:382-512`; `model_output._RULES` | `test_w3_cea_custom_and_stop_red` 7 RA; `test_w3_cea_rules_denylist...` 13 RA (m) | ~250 | all |
| G30 | W4CEC-5 | maj | `service/conversation_engine/writer_examples.py:23,53` | `test_w4_conversation_engine_c_safety::w4cec5` 5 RA (m) | ~60 | all |
| G31 | W4OFR-05 | maj | `service/onlyfans_read/repo.py:1017,1037-1048` | `test_w4_onlyfans_read_contract::w4ofr_03` RA (m) | ~60 | all except G40 |
| G32 | W4TI-01 | maj | `service/model_transport/pilot_supervised.py:100-108` | `test_w4_trace_isolation_pilot_foreign_provider_pg` RA (m) | ~50 | all except G08, G13, G15 |
| G33 | W5NL-5, W5NL-6, W5NL-7, W5NL-8 | maj (ARMED) | `service/native_pilot/ledger.py:781-858,1216-1330,1892-1894`; `control.py:74-82`; `engine.py:464-502` | `test_w5_nl_*` scripts: 3 assertion errors + `NameError 'result'` (m) | ~200 | all |
| G34 | W5RL-4, W5RL-5 | maj | `service/launch_personality/policy.py:22-82` | `test_w5_rl_persona_lint` 5 RA (m) | ~80 | all |
| G35 | W5TA-5, W5TB-1 | maj | `service/twin_bridge/coordination.py:821-826`; `worker.py:474-478,509-513` | scripts 1 FAIL + 2 FAIL (m) | ~100 | all |
| G36 | W5ED-3, W5ED-4 | maj | `ops/plan-c-supervision/delivery_history.py:272-318` | repro: conflict waived; cursor past now (m) | ~100 | all |
| G37 | W3RO-05 | maj | `service/unread_preservation.py:85-90`; `provider_connector.py:276-282` | `test_w6_media_webhook_misc_red::test_w3ro_05` RA (m) | ~80 | all |
| G38 | W6MW-7 | maj | `service/media_cotag.py:213-215` | `test_w6_media_webhook_misc_red::test_w6mw_7` RA (m) | ~50 | all |
| G39 | W6OC-7 | maj | `service/of_client.py:762-777` | `test_w6_of_client_red::test_w6oc7` RA (m) | ~80 | all |
| G40 | W4OFR-08, W4OFR-09, W4OFR-12 | minor batch | `service/onlyfans_read/auth.py:161`; `repo.py:282,292`; `contract.py:143-157`; `service.py:223` | 3 RA (m) | ~80 | all except G31 |
| G41 | W6AA-1, W6AA-2, W6AA-5, W6AA-6, W6AA-7, W6AA-8 | relay-deferred | `service/automations/_common.py:1062-1126,1777-1807,2581-2720`; `_of_integrity.py:152-168`; `_language.py:193-327` | `test_w6_automations_engine_a_red` 7 RA (m) | ~250 | all except G10 |
| G42 | W6AD-1, W6AD-2 | relay-deferred | `service/automations/pack_pricing.py:234-292,519-549` | `test_w6_automations_engine_d_red` 2 RA (m) | ~120 | all |
| G43 | W6AD-5, W6AD-8 | relay-deferred | `service/automations/pack_claim.py:70-92`; `native_pilot/media_claims.py:12,42`; `automations/fan_state.py:47-82` | 2 RA (m) | ~100 | all |
| G44 | W6AX-3, W6AX-5, W6AX-6 | relay-deferred | `service/automation_executor.py:394-409,1797-1801`; `moxy_replacement/routing.py:16-20` | `test_w6_auth_executor_red` 3 RA (m) | ~150 | all |

## Per-finding

`RA` red assert, `RD` red drift (ImportError), `G` green, `FAIL` script assertion, `RD*` drift but defect present (i).

| id | sev | bucket | group | test | result at head | note |
|---|---|---|---|---|---|---|
| CVA-01 | blk-cond/major | blk | G07 | `test_w1_commands_vault_ai_classification` uncertain | RA (m) | uncertain item keeps internal memberships |
| CVA-05 | major | maj | G25 | same file operator-removed | RA (m) | re-filed after first commercial pass |
| CVA-06 | minor | YAGNI-n/a | — | same file SQLite window | RA (m) | SQLite-only; YAGNI |
| L3-01 | blocker latent | blk | G06 | `test_w1_fans_api_adapter_pg` ×4 | RA (m) | unready item becomes catalogue |
| L3-02 | blocker latent | blk | G06 | same ×4 | RA (m) | DRM item becomes catalogue |
| L3-01b | blocker | blk | G06 | same retired item | RA (m) | retired item resurrected |
| L3-03 | blocker | blk | G06 | same vanished item | RA (m) | vanished not retired |
| L3-05 | major | maj | G24 | same read-before-Retry-After | RA (m) | persisted retry ignored |
| L3-06 | major | maj | G24 | same 401 re-polled | RA (m) | 401 terminal |
| L3-07 | major | maj | G24 | same silent new job | RA (m) | exhausted job restarted |
| L3-08 | minor | maj | G24 | same 3600<7200 | RA (m) | retry shortened; rides G24 |
| F1 | major | sec-money maj | G13 | `test_w1_model_transport_pg` F1 | RA (m) | non-ASCII Retry-After → unknown |
| F2 | major | sec-money maj | G13 | same F2 | RA (m) | open, not handover_pending |
| F3 | blocker | blk | G09 | same F3 ×4 | RA (m) | key/prompt persisted |
| F5 | major | sec-money maj | G13 | same F5 | RA (m) | due retry starved |
| F02 | major | sec-money maj | G14 | `test_w1_onlyfans_runtime_category_single_price` ×2 | RA (m) | priced off category row |
| F04 | major | maj | G26 | `test_w1_onlyfans_runtime_mixed_family_ready` | RA (m) | no owner_id crash |
| T17 | major | maj | G27 | `test_w1_tests_runner_collection` ×2 | RA (m) | 12 nested files uncollected |
| W2S-02 | blocker LIVE | blk | G01 | `test_w2_conversation_engine_sale_pg::w2s02` | RD* (m) | base DOC-10 present (i) |
| W2S-05 | blocker | blk | G06 | `test_w2_conversation_engine_sale_drm_pg` | RD* (m) | base F01 present (i) |
| W2SC-02 | blocker | blk | G02 | `test_w2_scope_commercial_route_pg::emergency_stop` | RD* (m) | pilot-stop variant present (i) |
| W2OR-01 | major | sec-money maj | G14 | `test_w2_onlyfans_runtime_pg::w2or01` | RD* (m) | base F02 measured (m) |
| W2OR-03 | major | maj | G28 | `::w2or03` | RD* (m) | manual half present (i) |
| W2OR-04 | blocker LIVE | in-flight | — | `::w2or04` | RD* (m) | F5 in flight; connections.py:332-344 |
| W3CEA-01 | major | maj | G29 | `test_w3_cea_custom_and_stop_red::explicit_stop` | 3 RA (m) | opt-out ignored |
| W3CEA-04 | major | maj | G29 | `::custom_offer` | 4 RA (m) | custom offer claims |
| W3CEA-05 | major | maj | G29 | `test_w3_cea_rules_denylist...` | 13 RA (m) | tip/denylist rules |
| W3CEA-07 | blocker latent | blk | G11 | `test_w3_cea_pending_evidence_pg` | RD* (m) | `held` only; defect m |
| W3DC-01 | blocker LIVE | closed | — | head `test_w3_delta_curl_80_capture` | 25 G (m) | #85 + legacy #87 |
| W3DC-02 | major | sec-money maj | G21 | ad-hoc wrong static override | RA (m) | accepted; no head test |
| W3DC-03core | major | closed | — | head capture tests | G (m) | #84 + legacy #87 |
| W3DC-04 | major | closed | — | head cookie-form test | G (m) | #86 |
| W3DC-06 | minor | closed | — | head sign-echo tests | G (m) | #86 |
| W3DC-07 | — | closed | — | head legacy tests | G (m) | #87 |
| W3DC-08 | — | closed | — | head legacy proxy tests | G (m) | #87 |
| W3DR-01 | blocker | YAGNI-n/a | — | `test_w3_delta_r0_live_pg::w3dr01` | G (m) | never on head lineage |
| W3DR-02 | major | sec-money maj | G15 | `::w3dr02` | RA (m) | crash after release strands |
| CVA-10 | minor | YAGNI-n/a | — | `test_w3_runtime_content_misc_withdrawal` | RA (m) | YAGNI |
| W3RCB-01 rest | blocker | blk | G04 | `test_w3_rcb_history_page` | 3 RA, 2 G (m) | tip/deleted green by #83 |
| W3RCB-03 | major LIVE | blk | G03 | `test_w3_rcb_recent_inbox_retry_pg` | RA (m) | = W3RO-02 |
| W3RO-01 | blocker | blk | G03 | `test_w3_ro_recent_inbox_pg::fan_specific_refusal` | RA (m) | 117× bad read |
| W3TL-01 | blocker | blk | G08 | `test_w3_trace_ledger_pg` | RA (m) | second primary call |
| W3TL-02 | blocker | blk | G08 | same | RA (m) | fallback call |
| W3TSA-02 | blocker latent | blk | G10 | `test_w3_tsa_ladder_stop` ×3 | RA (m) | stop ladder |
| W4CEB-1 | major | sec-money maj | G16 | `test_w4_ceb_lease_lapse_settle_pg` | RA (m) | lease lapse settle |
| W4CEC-4 | major | blk | G05 | `test_w4_conversation_engine_c_safety::w4cec4` | 7 RA (m) | safety context |
| W4CEC-5 | major | maj | G30 | `::w4cec5` | 5 RA (m) | writer examples |
| W4OFR-05 | major | maj | G31 | `test_w4_onlyfans_read_contract::w4ofr_03` | RA (m) | contract |
| W4OFR-07 | major | sec-money maj | G23 | `test_w4_onlyfans_read_earnings_pg` | RA (m) | earnings |
| W4OFR-08 | minor | minor | G40 | `::w4ofr_01` | RA (m) | auth |
| W4OFR-09 | minor | minor | G40 | `::w4ofr_02` | RA (m) | repo |
| W4OFR-12 | minor | minor | G40 | `::w4ofr_04` | RA (m) | contract/service |
| W4TI-01 | major | maj | G32 | `test_w4_trace_isolation_pilot_foreign_provider_pg` | RA (m) | foreign provider |
| W3FA-03 | major | blk | G01 | `test_w4_trace_isolation_resolve_pg` ×2 | RA (m) | unscoped `_resolve` |
| W4SS-1 | blocker latent | blk | G10 | `test_w4_trace_supersede_seams_pg::w4ss1` | RA (m) | supersede seam |
| W5BB-1 | blocker LIVE | closed | — | head `test_w5_bb_login_review` | G (m) | #84 |
| W5BB-7 | minor | closed | — | same malformed sign | G (m) | #86 |
| W5BB-8 | minor | YAGNI-n/a | — | sweep `test_w5bb9` | RA at sweep test (m); test absent at head | scrub mangles; YAGNI |
| W3TL-26 | blocker | blk | G12 | `test_w5_lp_llm_client_spend::test_w3tl26` | RA (m) | spend cap |
| W3TL-07 | major | blk | G12 | `::test_w3tl07` | RA (m) | 3 POSTs; rides G12 |
| W4MA-05 | latent | blk | G12 | `::test_w4ma05` | RA (m) | NULL reserve |
| W5ME-1 | major | sec-money maj | G17 | `test_w5_me_legacy_reconnect_pg` | RA (m) | reservation |
| W5NL-5 | major | maj | G33 | `test_w5_nl_unresolved_lease` | error/assert (m) | lease unresolved |
| W5NL-6 | major | maj | G33 | `test_w5_nl_operator_stop` | error/assert (m) | stop undone |
| W5NL-7 | major | maj | G33 | `test_w5_nl_mode_switch_cooldown` | error/assert (m) | cooldown erased |
| W5NL-8 | major ARMED | maj | G33 | `test_w5_nl_offer_blocker` | `NameError 'result'` (m) | fail-open offer blocker |
| W5NR-5 | blocker-cond | blk | G05 | `test_w5_nr_safety_detect` ×8 | RA (m) | minor roleplay |
| W5NR-6 | major | blk | G05 | same ×3 | RA (m) | reported speech hides distress |
| W5RL-4 | major | maj | G34 | `test_w5_rl_persona_lint::refuses` | 5 RA (m) | tip/meetup retry |
| W5RL-5 | major | maj | G34 | same negation cases | RA (m) | negation bypass |
| W5TA-4 | major | sec-money maj | G22 | `test_w5_ta_prewire_unknown.py` | 2 FAIL (m) | prewire refusal → unknown |
| W5TA-5 | major | maj | G35 | `test_w5_ta_fence_fail_open.py` | 1 FAIL (m) | fence fails open |
| W5TB-1 | major | maj | G35 | `test_w5_tb_fail_open_gates.py` | 2 FAIL (m) | lease/cooldown fail open |
| W5ED-3 | major | maj | G36 | `repro_delivery_history.py` (a) | reproduces (m) | conflict waived |
| W5ED-4 | major | maj | G36 | same (b) | reproduces (m) | cursor past now |
| W6AX-3 | major | relay-deferred | G44 | `test_w6_auth_executor_red::test_w6ax3` | RA (m) | fence misses vault reminder |
| W6AX-5 | major | relay-deferred | G44 | `::test_w6ax5` | RA (m) | orphan cap |
| W6AX-6 | major | relay-deferred | G44 | `::test_w6ax6` | RA (m) | cooldown shortened |
| W6AX-7 | major | sec-money maj | G18 | `::test_w6ax7_*` | 2 RA (m) | master auth |
| W6AA-1 | blocker live-risk | relay-deferred | G41 | `test_w6_automations_engine_a_red` ×2 | RA (m) | minor terms disguised |
| W6AA-2 | blocker live-risk | relay-deferred | G41 | same | RA (m) | underage statement dropped |
| W6AA-5 | major | relay-deferred | G41 | same | RA (m) | typo layer restores word |
| W6AA-6 | major | relay-deferred | G41 | same | RA (m) | AI identity questions |
| W6AA-7 | major | relay-deferred | G41 | same | RA (m) | curly apostrophe meetup |
| W6AA-8 | major | relay-deferred | G41 | same | RA (m) | Spanish lane brakes |
| W6AD-1 | blocker | relay-deferred | G42 | `test_w6_automations_engine_d_red` | RA (m) | ladder default on |
| W6AD-2 | blocker | relay-deferred | G42 | same | RA (m) | discount after refusal |
| W6AD-5 | major | relay-deferred | G43 | same | RA (m) | media claim boundary |
| W6AD-8 | major | relay-deferred | G43 | same | RA (m) | corrupt fan-state wipe |
| W6MW-21 | major | blk | G05 | `test_w6_media_webhook_misc_red::test_w6mw_21` | RA (m) | stock refusal len 40 |
| W3RO-05 | major | maj | G37 | `::test_w3ro_05` | RA (m) | unread restore |
| W6MW-7 | major | maj | G38 | `::test_w6mw_7` | RA (m) | URL.replace |
| W6MW-16 | major | sec-money maj | G19 | `::test_w6mw_16` | RA (m) | unparseable store overwritten |
| W6OC-14 | major | sec-money maj | G20 | `test_w6_of_client_red::test_w6oc14` | RA (m) | single-sample verify |
| W6OC-7 | major | maj | G39 | `::test_w6oc7` | RA (m) | iter_messages no progress |
| W6HR-1 | blocker-cond LIVE | in-flight | — | none | untested (m) | F4; worker 943f3851 |
| W6SI-18 | major | closed | — | `test_signin_bind_loopback.py` | G (m, #88) | :8479 loopback |
| F0 | — | closed | — | `test_check_harness.py` | G (m, #89) | check harness |

## Closed (already merged)

| id | merge | evidence |
|---|---|---|
| W5BB-1 | #84 `b9a8c516` | head `test_w5bb1_*` 2/2 G (m) |
| W3DC-01 | #85 `6ca2172a` + #87 `9124b50d` | head capture file 25 G (m) |
| W3DC-03 core | #84 `b9a8c516` + #87 legacy | owner/auth_id tests G (m) |
| W3DC-04 | #86 `bdc6ad2b` | cookie-header test G (m) |
| W3DC-06 | #86 `bdc6ad2b` | sign-echo tests G (m) |
| W3DC-07 | #87 `9124b50d` | legacy fetch tests G (m) |
| W3DC-08 | #87 `9124b50d` | legacy proxy tests G (m) |
| W5BB-7 | #86 `bdc6ad2b` | malformed sign test G (m) |
| W6SI-18 | #88 `96609e23` | loopback test G (m) |
| F0 | #89 `91e32acc` | harness test G (m) |

## In-flight

| id | fix | state |
|---|---|---|
| W6HR-1 | F4 | blocker-cond; no red test anywhere (m); code LIVE on worker 943f3851 (i) |
| W2OR-04 = W2SC-22 | F5 | blocker LIVE; RD ImportError, defect measured at `connections.py:332-344` (m) |

## Drift-unknown (code absent at head; lives on commercial-integration)

25 findings: W2S-01, W2S-03, W2S-04, W2S-07, W2S-08, W2S-11, W2S-12, W2SC-01, W2SC-03, W2SC-04, W2SC-05, W2SC-06, W2DB-01, W2DB-02, W2DB-03, W2DB-04, W2DB-05, W2DB-07, W2FA-01, W2FA-03, W2FA-05, W2FA-06, W2OR-02, W2OR-18, TC-02. All RD ImportError/collection error at head (m); defect status on commercial-integration line unknown (i). Not queued.

## YAGNI / n-a

- `CVA-06` minor (SQLite-only), `CVA-10` minor (withdrawn DOC-20), `W5BB-8` minor (scrub mangles) — YAGNI (m RA except W5BB-8’s test is gone at head).
- `W3DR-01` blocker — n/a, defect never existed on head lineage; test G (m).

## Untested follow-ups (no red test; not counted above)

- Relay/wcfi: W6AE-1..4 (age guard/mass send), W6AX-1/-2 (triple mass send), W6CE-1 (SSE principal), W6AA-3 (`_outbound._guard` no age) — relay-deferred; write tests before re-enable.
- Live-risk native: W6OC-1 (OF-native welcome no owner fence), W6VA-1 (describer “do not refuse”), W6VA-2 (sellability ignores confidence), W6VS-12 (vault upload no stop check), W6MW-77/-78 (unread restore live read), W6SI-2 (sign-in proxy/owner caller), W6C1-1, W5BB-3/-4 (session recording/shared proxy), W5ME-9a (proxy inventory fence inert), W4MT-01 (model transport), W5NR-3/W5LP-1 (paid re-call).
- Capture-rest follow-ups from FIX-QUEUE: F1b (`_adopt_into_account` no auth_id), F2b (legacy relay paste-curl plain urllib), F2c (Browserbase `"proxies": True`), F2d (corrupt `proxies.json` → direct egress). F2b is relay; F1b/F2c/F2d are security/direct-egress.

## Ambiguities resolved

1. **Green vs closed**: the six merges fixed W5BB-1/W3DC-01/03/04/06/07/08/W5BB-7, but the explicit list books them CLOSED, so fixed-by-us is 0 and they appear once under closed.
2. **Relay rule applied by path**: `service/automations/`, `automation_executor.py`, wcfi, legacy relay → relay-deferred even when the finding is a money blocker (W6AD-1/2) or live-risk (W6AA-1/2). W6AA-1 also reaches native `ai_chatter`; flagged in the group note, not double-counted.
3. **W3DC-02**: sweep test passes only vacuously (calls the new required `account=` signature and catches any exception). Direct probe shows the wrong static override is accepted → still red (m).
4. **W6HR-1 / W2OR-04**: listed IN-FLIGHT, not queued, even though W2OR-04 measures red.
5. **F-rest items** (F1b/F2b/F2c/F2d) are notes in FIX-QUEUE, not table rows; listed as untested follow-ups, not counted.
