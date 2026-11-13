# FIX-QUEUE (backend)

**Buckets (findings/groups; triage @91e32acc, recounted @77b6d33d 2026-11-13T05:4xZ):**
blk 17/8 (G01 G02 G04 G05 merged) · sec-money maj 14/11 · maj 28/17 (+G05-PR2) · minor 4/2 (+W6HR-1c) · relay-deferred 13/4 · fixed-by-team 0 · closed 19 (+G01 G02 G04 G05, F5 W2OR-04) · in-flight 1 (F4 W6HR-1: 1a #100 merged, 1b pending) · drift-unknown 25 · YAGNI 4. **Open queued: 76** (≥50 → review lanes stay paused).
- Queued from fixes 2026-11-13: **G05-PR2 (maj)** engine has no numeric-age path ("im 16 tbh" caught natively only) + engine "step-sis"/"stepbros", native "im a kid", obfuscated ages, non-English; shared minor module/parity table; owner gates (b) minor bypasses shadow mode? (c) wire output_violation (G14). **W6HR-1c (minor)** new-fan trailing tip counted one poll late (Codex FCF-01).

- Source: [TRIAGE-91e32acc.md](TRIAGE-91e32acc.md) (DeepSeek Flash, read-only worktree). Opus spot-check 5/5 still red at 29f28729 (measured): W3RO-01, W6AX-7×2, W6OC-14, W4OFR-07, T17×2.
- Order = group order G01→G44 (blk, sec-money maj, maj, minor batch, relay-deferred last = "before relay re-enable" gate). One PR per group (root cause).
- Slot rule: next group must share no product file with the in-flight fix. G03 (recent_inbox.py) waits for F4 (same file). Next pick: G01 (ownership.py / fans_api_adapter), disjoint with F4 (inferred).
- Follow-ups from fix reviews (queued, minor): FLG3-01 Moxy `current()` cancels on unreadable ownership evidence → event later `stale` w/o operator case (G01b); FLG2-05 G01b known-red HTTP ownership tests → macOS skipif (after F0b); W6HR-2b tip-only re-read (F4). From G02 review: **G02-01 (maj, send-safety)** `_disable_sends` (fans_api_adapter/events.py:483) refused by text_delivery.py:282 when policy drifted wider than scope → sends not disabled; clamp blocked by :304-307; **G02-02 (minor)** connections.py:319/:364 write delivery policy without an OfDeliveryPolicyChange audit row. Flash G02 minors: FLG02-01 stop clamp ratchets the cap (widen→activate can't recover the pre-stop cap; tightens only, accept or add a stop-only writer); FLG02-02 `daily_limit_units` not clamped at stop (activation clamps it). Relay gate +: FLG2-07/G01-01 priced relay send on corrupt evidence (server.py:5141).
- Harness follow-up **F0e (minor)**: premerge counts opt-in pytest skips (e.g. test_moxy_conversation_repair_pg `set MOXY_REPAIR_TRANSCRIPT_OUT to dump transcripts`) as INCOMPLETE → fast INFRA; allow-list known opt-in skip reasons instead of blanket ALLOW_SKIP=1. Also: `GREEN (N deferred)` leaves the closest touched-area tests unrun; orchestrator runs deferred lists by hand for send/ownership fixes until a smarter order (most-relevant first) lands.
- Team base reds (red at 91547788, measured; known-red in G01b, not ours to fix unless a group owns them): test_moxy_input_supersession_pg::test_explicit_retirement_keeps_old_inputs_in_history_only (moxy_event_key_conflict); test_moxy_multicreator_load_pg::test_three_creators_concurrent_mixed_load (phase A timeout + engine exceptions); history_capacity_turns script (dropped from known-red by F0d).
- Drift-unknown (code only on commercial-integration) + 22 untested live-risk ids: not queued; need a red test first.

---

## History (pre-re-sort queue, kept for ids/notes)

# FIX-QUEUE — lane red tests vs backend team head

- Team head: `origin/claude/commercial-r0-r3-20261111` = **a253c84c** (#83). Date 2026-11-12.
- Env: macOS, throwaway PG 18 on 127.0.0.1:55640 (/tmp/fix-sweep-pg, stopped after), venv python, `nice -n 10`, serial, pytest `-q -rA -p no:cacheprovider`, 600 s timeout per file. No stack DB / :8787 / :3001 / :8479 / onlyfans.com touched. Worktree /tmp/fix-sweep, branch jaka/fix-sweep-20261112 (commit 22fbbe79: 52 copied test files only). Raw output /tmp/fix-sweep-out/raw/.
- Labels: m measured, i inferred, g guess.

## Ancestry (read first)
- **a2f1dda6 is NOT an ancestor of a253c84c** (m). merge-base = e8e90954. The 43 commits e8e90954..a2f1dda6 (0134–0136 migrations, catalogue terms, history import, ownership gate, commercial route, price_intent, commercial_terms_fixture, history_support) live only on `origin/claude/commercial-integration-20261111` (m).
- Head = e8e90954 + f5205ddd (#80) + c48eb755 (#82) + a253c84c (#83) (m). These three are the only possible team fixes. `git log a2f1dda6..a253c84c` = those 3.
- Running build 8d0001f9 has the same service code as head except #83's files (m) → OPEN-FINDINGS "running 8d0001f9" notes used for drift judgements.
- W3DR-01 pin 66a1da4a (cristina-recovery-tools) is not an ancestor either (m).
- file:line below = the lane FINDINGS cite at the lane pin. † = file differs a2f1dda6 vs head (lines may shift, i).

## Counts
- Tests (cases): **231** = GREEN **4** (2 by team a253c84c; 2 defect never on head lineage) · RED-ASSERT **140** · RED-DRIFT **44** · GUARD-GREEN **43** (m). Plus W5ED repro script: 2 checks, both reproduce (m).
- Findings (rows): **120** = fixed-by-team **0** fully (W3RCB-01: 2/5 cases fixed by a253c84c, rest still red → queue) · queue **94** (incl. 6 drift-with-defect-present, W6HR-1 with no test) · drift-unknown **25** (code absent at head; defect lives on commercial-integration) · n/a **1** (W3DR-01, not on head lineage).

## Table
Result codes: RA = RED-ASSERT, RD = RED-DRIFT, G = GREEN, GG = GUARD-GREEN. Group from jaka/open-findings-20261111:docs/OPEN-FINDINGS.md.

| id | lane | sev | test file::function(s) | result | fixed / queue | product file:line | group |
|---|---|---|---|---|---|---|---|
| CVA-01 | W1 commands-vault-ai | blk-cond/major | test_w1_commands_vault_ai_classification (uncertain item) | RA (m) | queue | vault_auto_folders.py:119-133 | G4 |
| CVA-05 | W1 commands-vault-ai | major | test_w1_commands_vault_ai_classification (operator-removed re-filed) | RA (m) | queue | vault_auto_folders.py:144-145 | rest |
| CVA-06 | W1 commands-vault-ai | minor | test_w1_commands_vault_ai_classification (SQLite JSONB) | RA (m) | queue | vault_auto_folders.py automatic_window | YAGNI |
| L3-01 / L3-02 | W1 fans-api-adapter | blocker (latent) | test_w1_fans_api_adapter_pg ×4 params (DID NOT RAISE) | RA (m) | queue | vault_sync.py:270-281, :57; vault_management.py:285-315 † | G4 |
| L3-01b | W1 fans-api-adapter | blocker | test_w1_fans_api_adapter_pg (retired item resurrected) | RA (m) | queue | vault_sync.py:270-281 | G4 (i, with L3-01) |
| L3-03 | W1 fans-api-adapter | blocker | test_w1_fans_api_adapter_pg (vanished not retired) | RA (m) | queue | vault_sync.py:294-312 | G4 |
| L3-05 | W1 fans-api-adapter | major | test_w1_fans_api_adapter_pg (read before Retry-After) | RA (m) | queue | vault_sync.py:194-212, :253 | G5 |
| L3-06 | W1 fans-api-adapter | major | test_w1_fans_api_adapter_pg (401 re-polled 4×) | RA (m) | queue | vault_sync.py:240-256 | rest |
| L3-07 | W1 fans-api-adapter | major | test_w1_fans_api_adapter_pg (silent new job) | RA (m) | queue | vault_sync.py:172-195 | rest |
| L3-08 | W1 fans-api-adapter | minor | test_w1_fans_api_adapter_pg (3600 < 7200) | RA (m) | queue | protocol.py:66,77 † | G5 |
| F1 | W1 model-transport | major | test_w1_model_transport_pg (non-ASCII Retry-After → unknown) | RA (m) | queue | model_transport/openrouter.py:36-48, :74-99 | G5 |
| F1b | — | W3DC-03 rest: `_adopt_into_account` (Incogniton/Playwright) no auth_id check; enrollment curl branch (sink covers) |
| F2b | — | W3DC-01 rest (F2-05): legacy relay `from_curl` (server.py paste-curl, session_bootstrap.py ~:428) still plain urllib for 2313.js; W3DC-07/08 not applied there; proxies.json binding |
| F2c | — | N3 (pre-existing #82): browserbase_login.py `"proxies": True` → remote login egresses via Browserbase proxies, not account proxy; pass job.proxy as external proxy or refuse Browserbase for proxied accounts |
| F2d | — | corrupt/mis-shaped proxies.json → load() returns empty → "no proxy" → direct egress (proxies.py:49-61); fail closed on exists-but-unparseable, atomic save; Fable: after F6 |
| F2 | W1 model-transport | major | test_w1_model_transport_pg (open, not handover_pending) | RA (m) | queue | model_runtime.py:221-230, :267-271 | G5 |
| F3 | W1 model-transport | blocker | test_w1_model_transport_pg ×4 (sk-or-v1 / prompt text persisted) | RA (m) | queue | openrouter.py:57-58; model_runtime.py:288 | G11 |
| F5 | W1 model-transport | major | test_w1_model_transport_pg (due retry starved) | RA (m) | queue | pilot_supervised.py:375-382 † | G5 |
| F02 | W1 onlyfans-runtime | major | test_w1_onlyfans_runtime_category_single_price ×2 (1700) | RA (m) | queue | vault_management.py:485-491 † | G6 |
| F04 | W1 onlyfans-runtime | major | test_w1_onlyfans_runtime_mixed_family_ready ('OfFansConnection' has no owner_id) | RA (m) | queue | vault_management.py:278-282 † | rest |
| T17 | W1 tests | major | test_w1_tests_runner_collection ×2 (12 nested files uncollected) | RA (m) | queue | scripts/run-service-tests.py:111,116 | rest |
| W2S-01 | W2 sale | blocker (terms-only) | test_w2_conversation_engine_sale_pg::w2s01a ×2, w2s01b | RD ImportError (m) | drift-unknown: code absent at head (i) | commerce.py:368-434 † | G3 |
| W2S-02 | W2 sale | blocker (LIVE carry-over DOC-10) | test_w2_conversation_engine_sale_pg::w2s02 | RD (m) | **queue — drift: defect present (i**, OPEN-FINDINGS 8d0001f9 y) | ownership.py:179-185 (DOC-10) | G1 |
| W2S-03 | W2 sale | blocker | test_w2_conversation_engine_sale_pg::w2s03 | RD (m) | drift-unknown: history_ownership.py absent at head (m) | history_ownership.py:126-132 | G1 |
| W2S-04 | W2 sale | blocker | test_w2_conversation_engine_sale_pg::w2s04b | RD (m) | drift-unknown: absent at head (m) | history_ownership.py:92-107 | G1 |
| W2S-05 | W2 sale | blocker | test_w2_conversation_engine_sale_drm_pg | RD (m) | **queue — drift: base F01 present (i**, vault_management.py:855-856 y at 8d0001f9) | catalogue_select.py:56-67 (absent at head, m) | G4 |
| W2S-07 | W2 sale | minor | test_w2_conversation_engine_sale_pg::w2s07 | RD (m) | drift-unknown (i) | engine.py:223-244 † | G1 |
| W2S-08 | W2 sale | major | test_w2_conversation_engine_sale_price_intent (+control) | RD (m) | drift-unknown: price_intent.py absent (m) | price_intent.py:13-26 | G6 |
| W2S-11 | W2 sale | major | test_w2_conversation_engine_sale_pg::w2s11 | RD (m) | drift-unknown: ownership_gate.py absent (m) | ownership_gate.py:48-51 | G1 |
| W2S-12 | W2 sale | minor | test_w2_conversation_engine_sale_pg::w2s12 | RD (m) | drift-unknown (m) | ownership_gate.py:17-32 | G1 |
| W2SC-01 | W2 scope | blocker | test_w2_scope_commercial_route_pg::terms_alone, never_mints | RD (m) | drift-unknown: commercial_scope.py absent (m) | commercial_scope.py:24-31 | G3 |
| W2SC-02 | W2 scope | blocker | test_w2_scope_commercial_route_pg::emergency_stop ×2 | RD (m) | **queue — drift: pilot-stop variant present (i)** | commercial_lifecycle.py:176-190 (absent); head pilot_scope.py:703-715 (m read) | G2 |
| W2SC-03 | W2 scope | major | test_w2_scope_commercial_route_pg::damaged_scope_row | RD (m) | drift-unknown (m) | pilot_scope.py:293-302; commercial_scope.py:17-23 | G3 |
| W2SC-04 | W2 scope | major | test_w2_scope_commercial_route_pg::send_admission | RD (m) | drift-unknown (i) | preparation.py:158-165 † | G3 |
| W2SC-05 | W2 scope | major | test_w2_scope_history_pg (429 fan) | RD (m) | drift-unknown: history_import.py absent (m) | history_import.py:508-518 | G5 |
| W2SC-06 | W2 scope | major | test_w2_scope_history_pg (spent source → complete) | RD (m) | drift-unknown (i) | preparation.py:416-420 † | G1,G5 |
| W2DB-01 | W2 db | minor | test_w2_db_migrations_pg::w2db01 | RD (m) | drift-unknown: migration absent (m) | 20261111_1300_commercial_scope_contract.py:96-97 | G3 |
| W2DB-02 | W2 db | major | test_w2_db_migrations_pg::w2db02 | RD (m) | drift-unknown (m) | 20261111_1400_commercial_catalogue_terms.py:26-81 | rest |
| W2DB-03 | W2 db | minor | test_w2_db_migrations_pg::w2db03 | RD (m) | drift-unknown (m) | …catalogue_terms.py:17,77 | rest |
| W2DB-04 | W2 db | minor | test_w2_db_migrations_pg::w2db04 ×2 | RD (m) | drift-unknown (m) | …catalogue_terms.py:39-40 | G3 |
| W2DB-05 | W2 db | major | test_w2_db_migrations_pg::w2db05 | RD (m) | drift-unknown (m) | …commercial_scope_contract.py:61-67 | G3 |
| W2DB-07 | W2 db | minor | test_w2_db_migrations_pg::w2db07 ×2 (+seed guard, RD) | RD (m) | drift-unknown (m) | 20261111_1300_history_coverage.py:22 | rest |
| W2FA-01 | W2 fans-api | blocker | test_w2_fans_api_adapter_pg::w2fa_01 ×2 | RD (m) | drift-unknown: message_facts.py absent (m) | message_facts.py:46-51; events.py:643-655 † | G1 |
| W2FA-03 | W2 fans-api | blocker | test_w2_fans_api_adapter_pg::w2fa_03 | RD (m) | drift-unknown (i) | events.py:564-567 † | G1 |
| W2FA-05 | W2 fans-api | major | test_w2_fans_api_adapter_pg::w2fa_05 | RD (m) | drift-unknown (m) | history_import.py:445-452 | G5 |
| W2FA-06 | W2 fans-api | major | test_w2_fans_api_adapter_pg::w2fa_06 | RD (m) | drift-unknown (i; base L3-08 still RA at head, m) | protocol.py:55,66,77 † | G5 |
| W2OR-01 | W2 onlyfans-runtime | major (F02 worse) | test_w2_onlyfans_runtime_pg::w2or01 (+control) | RD (m) | **queue — drift: base F02 present (m**, W1 F02 test RA) | vault_management.py:488-491 † | G6 |
| W2OR-02 | W2 onlyfans-runtime | major | test_w2_onlyfans_runtime_pg::w2or02 | RD (m) | drift-unknown: catalogue_terms.py absent (m) | media_delivery.py:43-54 † | G6 |
| W2OR-03 | W2 onlyfans-runtime | major | test_w2_onlyfans_runtime_pg::w2or03 (+control) | RD (m) | **queue — drift: manual half present (i**, 8d0001f9 y) | text_delivery.py:218-229, :635-641 † | G1 |
| W2OR-04 = W2SC-22 | W2 onlyfans-runtime | **blocker, LIVE** | test_w2_onlyfans_runtime_pg::w2or04 | RD (m) | **queue — drift: defect present (m**, head connections.py:332-344 sets runtime/policy enabled, no stop check) | onlyfans_runtime/connections.py:332-344 | G2 |
| W2OR-18 | W2 onlyfans-runtime | blocker | test_w2_onlyfans_runtime_pg::w2or18 | RD (m) | drift-unknown: commercial_lifecycle absent (m) | launch_operations.py:150-152 → commercial_lifecycle.py:184-186 | G2 |
| TC-02 | W2 tests-commercial | blocker (structure) | test_w2_tests_commercial_collection ×2 ("found []") | RD (m) | drift-unknown: test_commercial_terms_wording.py absent at head (m) | scripts/run-service-tests.py:111 | rest |
| W3CEA-01 | W3 cea | major | test_w3_cea_custom_and_stop_red::explicit_stop (3 RA, 1 GG) | RA (m) | queue | controls.py:98-100, :55-65 | G8 |
| W3CEA-04 | W3 cea | major | test_w3_cea_custom_and_stop_red::custom_offer (4 RA, 1 GG) | RA (m) | queue | claims.py:382-392 † | rest |
| W3CEA-05 (R11/R13) | W3 cea | major | test_w3_cea_rules_denylist_from_c2c09b01 (13 RA, 6 GG) | RA (m) | queue | claims.py:464-512 †; model_output._RULES † | rest (S1) |
| W3CEA-07 = W3RCB-08 | W3 cea | blocker (latent) | test_w3_cea_pending_evidence_pg | RD ImportError history_support (m) | **queue — drift: defect present (m**, head commerce.py:65-70 `status == "held"` only) | onlyfans_runtime/commerce.py:65-70 | G1 |
| W3DC-01 | W3 delta-curl-80 | blocker, LIVE | test_w3_delta_curl_80_capture::test_no_direct_urllib_egress_to_onlyfans | RA (m) | queue | session_bootstrap.py:522-527, :551-553 † | G11 |
| W3DC-02 | W3 delta-curl-80 | major | …::test_wrong_static_param_is_rejected | RA (m) | queue | session_bootstrap.py:554-564 † | rest |
| W3DC-03 | W3 delta-curl-80 | major | …::test_auth_id_cookie_must_match_user_id, test_x_of_rev_validated_before_url | RA ×2 (m) | queue | connection_api.py:117-127 †; session_bootstrap.py:505-571 † | G10 |
| W3DC-04 | W3 delta-curl-80 | major | …::test_cookie_header_form_not_silently_dropped | RA (m) | queue | session_bootstrap.py:271-273 † | rest |
| W3DC-06 | W3 delta-curl-80 | minor | …::test_sign_value_not_echoed_in_error | RA (m) | queue | session_bootstrap.py:532, :541 † | G11 |
| W3DR-01 | W3 delta-r0-live | blocker | test_w3_delta_r0_live_pg::w3dr01 gzip 429, gzip 200 | G (m; red at 66a1da4a, green at a2f + head) | n/a — defect never on head lineage (m) | (66a1da4a only) | G5 |
| W3DR-02 | W3 delta-r0-live | major | test_w3_delta_r0_live_pg::w3dr02 | RA (m) | queue | ledger.release → turn.handover (pilot path) † | G5 |
| CVA-10 = W3RCT-23 (DOC-20) | W3 runtime-content | minor | test_w3_runtime_content_misc_withdrawal | RA (m) | queue | vault_auto_folders.py:186-193 | YAGNI |
| W3RCB-01 | W3 runtime-core-b | blocker, LIVE reads | test_w3_rcb_history_page::newer_valid_inbound[old-tip, old-deleted] | G ×2 (m; red at a2f + c48eb755) | **fixed-by-team a253c84c (#83)** for tip/deleted | native_connector.py (messages loop) † | G7 |
| W3RCB-01 (rest) | W3 runtime-core-b | blocker | …[old-unsupported-formatting]; …paragraph_variants[`<p>hi</p>\n`, `<p class="x">hi</p>`] | RA ×3 (m) | queue | native_protocol.py history_text † | G7 |
| W3RCB-03 = W3RO-02 | W3 runtime-core-b | major, LIVE | test_w3_rcb_recent_inbox_retry_pg (30 calls/1800 s) | RA (m) | queue | recent_inbox.py:187-197 | G7 |
| W3RO-01 | W3 runtime-offers | blocker | test_w3_ro_recent_inbox_pg::fan_specific_refusal (BAD read 117×) | RA (m) | queue | recent_inbox.py:188-189, :198 | G7 |
| W3RO-02 | W3 runtime-offers | major | test_w3_ro_recent_inbox_pg::give_up_survives_restart | RA (m) | queue | recent_inbox.py:47-49, :95-97 | G7 |
| W3TL-01 (= W1-04b) | W3 trace-ledger | blocker | test_w3_trace_ledger_pg (second primary call) | RA (m) | queue | model_runtime / pilot_supervised replay † | G5 |
| W3TL-02 (= W1-04a) | W3 trace-ledger | blocker | test_w3_trace_ledger_pg (fallback call) | RA (m) | queue | model_runtime same-pass fallback | G5 |
| W3TSA-02 | W3 trace-send-authority | blocker (latent) | test_w3_tsa_ladder_stop ×3 | RA (m) | queue | runtime_stop.STRONGER vs send checks; automations/_common.py:378 | G8 |
| W4CEB-1 | W4 cee-b | major | test_w4_ceb_lease_lapse_settle_pg | RA (m) | queue | model_ledger.py:266,278-286,336-343 | G5 |
| W4CEC-4 | W4 cee-c | major | test_w4_conversation_engine_c_safety::w4cec4 (7 RA, control GG) | RA (m) | queue | safety_context.py:8-15, :32-38 | G14 |
| W4CEC-5 | W4 cee-c | major | test_w4_conversation_engine_c_safety::w4cec5 (5 RA, 1 GG) | RA (m) | queue | writer_examples.py:23, :53 | rest |
| W4OFR-05 | W4 onlyfans-read | major | test_w4_onlyfans_read_contract::w4ofr_03 (test # ≠ id) | RA (m) | queue | repo.py:1017, :1037-1048 | G1 |
| W4OFR-07 | W4 onlyfans-read | major | test_w4_onlyfans_read_earnings_pg | RA (m) | queue | earnings.py:66-74 | rest |
| W4OFR-08 | W4 onlyfans-read | minor | test_w4_onlyfans_read_contract::w4ofr_01 | RA (m) | queue | auth.py:161 | rest |
| W4OFR-09 | W4 onlyfans-read | minor | test_w4_onlyfans_read_contract::w4ofr_02 | RA (m) | queue | repo.py:282, :292 | rest |
| W4OFR-12 | W4 onlyfans-read | minor | test_w4_onlyfans_read_contract::w4ofr_04 | RA (m) | queue | contract.py:143-157; service.py:223 | rest |
| W4TI-01 | W4 trace-isolation | major | test_w4_trace_isolation_pilot_foreign_provider_pg | RA (m) | queue | model_transport/pilot_supervised.py:100-108 † | G10 |
| W3FA-03 | W4 trace-isolation | (W3) | test_w4_trace_isolation_resolve_pg ×2 | RA (m) | queue | fans_api_adapter `_resolve` / unscoped intake | G10 |
| W4SS-1 | W4 trace-supersede | blocker (latent) | test_w4_trace_supersede_seams_pg::w4ss1 | RA (m) | queue | approval.py:110-122 †; pilot_scope.py:373-374 † | G8 |
| W5BB-1 | W5 delta-browserbase | blocker, LIVE | test_w5_bb_login_review::test_w5bb1_* | RA (m) | queue | browserbase_login.py:284-289, :328-336 † | G10+G11 |
| W5BB-7 | W5 delta-browserbase | minor | test_w5_bb_login_review::test_w5bb4_* (name ≠ id; Sign echoed in log) | RA (m) | queue | session_bootstrap.py:539, :548 → browserbase_login.py:422 † | YAGNI |
| W5BB-8 | W5 delta-browserbase | minor | test_w5_bb_login_review::test_w5bb9_* (name ≠ id; scrub mangles) | RA (m) | queue | browserbase_login.py:108-113 † | YAGNI |
| W3TL-26 | W5 llm-profile-learning | blocker | test_w5_lp_llm_client_spend::test_w3tl26_* | RA (m) | queue | llm_client.py:1133-1151 | G11 |
| W3TL-07 | W5 llm-profile-learning | major | test_w5_lp_llm_client_spend::test_w3tl07_* (3 POSTs) | RA (m) | queue | llm_client.py:1121-1169 | G5 |
| W4MA-05 | W5 llm-profile-learning | latent (no real caller) | test_w5_lp_llm_client_spend::test_w4ma05_* | RA (m) | queue | llm_client.py `_reserve` NULL mode | G5 |
| W5ME-1 | W5 moxy-eval-proxy | major | test_w5_me_legacy_reconnect_pg | RA (m) | queue | reservation.py:20 | rest |
| W5NL-5 | W5 native-pilot-ledger | major | test_w5_nl_unresolved_lease (script) | RA (m) | queue | native_pilot ledger.py:853-858, :781-782 | rest |
| W5NL-6 | W5 native-pilot-ledger | major | test_w5_nl_operator_stop (script) | RA (m) | queue | ledger.py:1216-1220, :1892-1894 | rest |
| W5NL-7 | W5 native-pilot-ledger | major | test_w5_nl_mode_switch_cooldown (script) | RA (m) | queue | control.py:74-82; ledger.py:1312-1330 | rest |
| W5NL-8 (= W6-ORPH-1, ARMED offer-blocker) | W5 native-pilot-ledger | major (ARMED) | test_w5_nl_offer_blocker (script; NameError 'result') | RA (m) | queue | native_pilot/engine.py:464-467, :496-502 | G16 |
| W5NR-5 | W5 native-pilot-runner | blocker-cond | test_w5_nr_safety_detect::w5nr5 inbound + draft (8 RA) | RA (m) | queue | safety_detect.py:30-41, :161-190, :233-255 | G14 |
| W5NR-6 | W5 native-pilot-runner | major | test_w5_nr_safety_detect::w5nr6 (3 RA) | RA (m) | queue | safety_detect.py:64-66, :167-175 | G14 |
| W5RL-4 | W5 runtime-learning | major | test_w5_rl_persona_lint::refuses | RA (m) | queue | launch_personality/policy.py:29-66 | G15 |
| W5RL-5 | W5 runtime-learning | major | test_w5_rl_persona_lint::refuses (negation cases) | RA (m) | queue | policy.py:22-26, :82 | G15 |
| W5TA-4 | W5 twin-bridge-a | major | test_w5_ta_prewire_unknown (script, 2 FAIL) | RA (m) | queue | platform.py:336-350 | G5 |
| W5TA-5 | W5 twin-bridge-a | major | test_w5_ta_fence_fail_open (script, 1 FAIL) | RA (m) | queue | coordination.py:821-826 | G16 |
| W5TB-1 | W5 twin-bridge-b | major | test_w5_tb_fail_open_gates (script, 2 FAIL) | RA (m) | queue | worker.py:474-478, :509-513 | G16 |
| W5ED-3 | W5 engine-deploy | major | docs/review/W5-engine-deploy/repro_delivery_history.py (conflict in resolved interval ACCEPTED) | RA (m, repro) | queue | delivery_history.py:272-277, 293-296 | rest |
| W5ED-4 | W5 engine-deploy | major | same repro (cursor past now) | RA (m, repro) | queue | delivery_history.py:304-318 | rest |
| W6AX-3 | W6 auth-executor | major | test_w6_auth_executor_red::test_w6ax3_* | RA (m) | queue | moxy_replacement/routing.py:16-20 † ↔ automation_executor.py:553-565 | rest |
| W6AX-5 | W6 auth-executor | major | …::test_w6ax5_* | RA (m) | queue | automation_executor.py:1797-1801 | G5 |
| W6AX-6 | W6 auth-executor | major | …::test_w6ax6_* | RA (m) | queue | automation_executor.py:394-409 | rest |
| W6AX-7 | W6 auth-executor | major | …::test_w6ax7_admin_list_users_*, test_w6ax7_impersonate_* | RA ×2 (m) | queue | auth.py:817, :938-944, :1135-1148 | G10+G11 |
| W6AA-1 | W6 automations-engine-a | **blocker, live-risk** | test_w6_automations_engine_a_red::w6aa1 ×2 | RA (m) | queue | automations/_common.py:2581-2641; _language.py:193-199 | G14 |
| W6AA-2 | W6 automations-engine-a | **blocker, live-risk** | …::w6aa2 | RA (m) | queue | _of_integrity.py:152-153, :168 | G14 |
| W6AA-5 | W6 automations-engine-a | major | …::w6aa5 | RA (m) | queue | _common.py:1777-1807 | G14 |
| W6AA-6 | W6 automations-engine-a | major | …::w6aa6 | RA (m) | queue | _common.py:1062-1063, :1115-1126 | G14 |
| W6AA-7 | W6 automations-engine-a | major | …::w6aa7 | RA (m) | queue | _common.py:2714-2720 | G14 |
| W6AA-8 | W6 automations-engine-a | major | …::w6aa8 | RA (m) | queue | _language.py:283-295, :324-327 | G8 |
| W6AD-1 | W6 automations-engine-d | blocker | test_w6_automations_engine_d_red::pack_price_ladder_is_not_default_on | RA (m) | queue | pack_pricing.py:234-250, :292 | G15 |
| W6AD-2 | W6 automations-engine-d | blocker | …::pack_ladder_never_discounts_after_refusal | RA (m) | queue | pack_pricing.py:268-280, :519-549 | G15 |
| W6AD-5 | W6 automations-engine-d | major | …::engine_media_claim_boundary_* | RA (m) | queue | pack_claim.py:70-92; native_pilot/media_claims.py:12, :42 | G14 |
| W6AD-8 | W6 automations-engine-d | major | …::fan_state_write_does_not_wipe_corrupt_blob | RA (m) | queue | fan_state.py:47-48, :77-82 | rest |
| W6MW-21 | W6 media-webhook-misc | major | test_w6_media_webhook_misc_red::test_w6mw_21_* | RA (m) | queue | vision.py:66 | G14 |
| W3RO-05 | W6 media-webhook-misc | major | …::test_w3ro_05_* | RA (m) | queue | provider_connector.py:276-282 † → unread_preservation.py:85-90 | G7 |
| W6MW-7 | W6 media-webhook-misc | major | …::test_w6mw_7_* (URL.replace) | RA (m) | queue | media_cotag.py:213-215 | rest |
| W6MW-16 | W6 media-webhook-misc | major | …::test_w6mw_16_* | RA (m) | queue | secrets_store.py:230-247 | G10+G11 |
| W6OC-14 | W6 of-client | major | test_w6_of_client_red::test_w6oc14_* | RA (m) | queue | of_signer.py:93-125 | rest |
| W6OC-7 | W6 of-client | major | test_w6_of_client_red::test_w6oc7_* | RA (m) | queue | of_client.py:762-777 | rest |
| W6HR-1 | W6 history-read | blocker-cond, LIVE code | **none** (lane commits only FINDINGS + ledger, m) | — | queue (untested) | native_connector.py:204-220; store.py:878-895 (shipped by a253c84c) | G8 |

### GUARD-GREEN (designed to pass; not team fixes) (m)
L3-P1 (fans_api_adapter) · F4 + F5-control (model_transport) · F02 control · T05/T06/T07 ×10 (test_w1_tests_model_retry_gaps_pg) · W3CEA custom + stop controls · R11/R13 6 green params (green at a2f too) · W3DC baseline · W3DR-01 identity-encoding control · W3RCB control_valid_page_reads · W3TL-23 ledger cap ×3 (test_w4_ceb_ledger_cap_pg, green at a2f) · W4CEC-4 control · W4CEC-5 1 param (green at a2f) · W4 supersede seams ×4 (green at a2f) · W5LP reserve_fits · W5NR controls ×2 · W5RL baseline ×3 · W5TA ok checks ×3. Total 43.

### Drift detail
- 44 RD = all 41 W2 functions (collection ImportError: prepare_commercial_offer, commercial_terms_fixture, conversation_engine.price_intent, history_support, db.onlyfans_catalogue_terms_models; 4 of them are controls/guards that also cannot run) + TC-02 ×2 + W3CEA-07 (m).
- Defect present at head → queue: W2OR-04/W2SC-22 (m), W3CEA-07/W3RCB-08 (m), W2OR-01 base F02 (m), W2SC-02 pilot-stop variant (i), W2OR-03 manual half (i), W2S-02 base DOC-10 (i), W2S-05 base F01 (i).
- Rest (25): the code is not on head, so the defect is gone at head (i). It is still present on commercial-integration-20261111 and must be fixed there before that line merges.

## Live-risk applicability (STATE.md "Live-risk against RUNNING build 8d0001f9")
| live-risk id | red test at head |
|---|---|
| W6HR-1 | **none** (no red test exists anywhere, m). a253c84c's W3RCB fix is the mechanism: rows are skipped, never `_record`ed (i) |
| W6OC-1, W6VA-1/-2, W6VS-12, W6MW-77/-78, W6AE-1..4, W6SI-18/-2, W6C1-1 | none in the swept lanes (m) |
| CONTENT SAFETY W6AA-1 / W6AA-2 | test_w6_automations_engine_a_red::test_w6aa1_minor_terms_are_never_disguised, test_w6aa1_spanish_minor_terms_are_never_disguised, test_w6aa2_underage_self_statement_is_reported_not_dropped — RA |
| W6AA-3 | none |
| ARMED offer-blocker (= W5NL-8) | test_w5_nl_offer_blocker.py (script) — RA |
| W6AD-16, W6OC-72, W6MW-9 (public-repo ids) | none (policy, owner). Note: a real-looking account id also sits in pre-existing repo tests (test_native_pilot_*.py, test_fastt_reply_budget.py); value withheld here |
| W6AX-1 / W6AX-2 | none (W6AX-3/-5/-6/-7 are tested, RA) |
| W5BB-1 | test_w5_bb_login_review::test_w5bb1_capture_of_another_creator_is_not_offered — RA |
| W5BB-3 / W5BB-4 | none (lane's `test_w5bb4_*` actually covers W5BB-7) |
| W5ME-9a | none (test_w5_me_legacy_reconnect_pg covers W5ME-1) |
| W2OR-04 | test_w2_onlyfans_runtime_pg::test_w2or04_* — RD ImportError; defect present at head (m, connections.py:332-344) |
| W3DC-01 | test_w3_delta_curl_80_capture::test_no_direct_urllib_egress_to_onlyfans — RA |
| ARMED G14/OQ-25 + W5NR-5 | test_w5_nr_safety_detect::test_w5nr5_minor_roleplay_inbound_is_held, test_w5nr5_minor_roleplay_draft_is_violation — RA |
| ARMED W5NR-3 / W5LP-1 | no dedicated test. The closest is test_w5_lp_llm_client_spend::test_w3tl07_read_timeout_not_resent_and_reservation_kept (RA, retry re-sends). OPEN-FINDINGS cites this file for W5LP-1 but it has no W5LP-1 function (m) |
| ARMED W4MT-01 | no dedicated test (W4 model-transport-commands lane not in manifest). Related RA: W3TL-01/-02 (test_w3_trace_ledger_pg), W1 F1/F2 |

## Fix order (decided by fix-orchestrator 2026-11-12; jaka/* findings first, then live-risk, then groups)
| # | fix branch | findings | why first |
|---|---|---|---|
| F1 | fix/W5BB-1-capture-owner-binding | W5BB-1 + W3DC-03 (same root: capture not bound to account owner) | blocker, LIVE, cross-tenant, our #80/#82; + adds scripts/premerge.sh |
| F2 | fix/W3DC-01-no-direct-egress | W3DC-01 | blocker, LIVE since #80/#82, direct OF egress from host IP |
| F3 | fix/W3DC-06-secret-echo | W3DC-06 + W5BB-7 (Sign echoed in error/log) + W3DC-04 if small | our jaka findings (cookie echo) |
| F0 | fix/F0-check-harness | scripts/check.sh fast/live/full (owner 22:4xZ) | MERGED #89 → 91e32acc; merge gate from now |
| F0c | fix/F0c-known-red-release-env | known-red 3 env-gated release rehearsal tests (premerge) | MERGED #90 → 29f28729; Flash MERGE, 2 lows → F0b |
| F5 | fix/W2OR-04-activate-stop-check | W2OR-04/W3RCA-02 Activate vs operator stop + scope | MERGED #91 → a1151b1f; rwl r1-r3; fast GREEN 882s (loaded) |
| OWN-b | fix/OWN-b-signin-loopback | W6SI-18 (:8479 on all interfaces) | owner 22:1xZ (b): small, do next |
| F4 | fix/W6HR-1-skipped-inbound-input | W6HR-1 (new red test) | blk-cond, LIVE on worker (#83, ours) |
| F5 | fix/W2OR-04-activate-stop-check | W2OR-04 = W2SC-22 | blocker, LIVE path (Activate re-enables stopped account) |
| F5b | fix/W2OR-04b-status-stopped | F5-02: status shows activate for operator-stopped account | follow-up of F5 |
| F0b | fix/F0b-check-hardening | premerge ONLYFANS_TEST_PG_URL loopback guard; full skip-only detection; read_only PG test; test gaps (FINDINGS r3 D rows) | follow-up of F0 |
| F6 | fix/W3RO-01-recent-inbox-retry | W3RO-01 + W3RCB-01 rest | blocker, LIVE reads (infinite per-fan retry / page refusal) |
| F7 | fix/W6AA-1-content-safety | W6AA-1, W6AA-2 (+ W5NR-5) | G14 blocker, gate = sending off |
| F8 | fix/W5NL-8-offer-blocker-fail-closed | W5NL-8 (+ W5TA-5, W5TB-1) | G16 ARMED |
| F9 | fix/W3TSA-02-stop-ladder | W3TSA-02, W4SS-1 | G8 blockers |
| F10 | fix/W3TL-01-ledger | W3TL-01, W3TL-02 | G5 blockers (money) |
| F11 | fix/W6AD-1-money-asks | W6AD-1, W6AD-2 | G15 blockers |
| F12 | fix/L3-01-unsellable | L3-01..03, CVA-01 | G4 blockers |
Then G5/G7/G10 majors.

### Gate: before relay re-enable (owner 22:2xZ, OQ-27 answered)
Relay (service/server.py, automations, wcfi) is NOT in prod; it may be reused. These are NOT live-risk. Fix them after the engine/live items, before anyone turns the relay back on. Never delete relay code.
| # | findings | note |
|---|---|---|
| R1 | W6AE-1..4 (wcfi asks age, stores "16") | cheap age guard first (refuse/hold if a fan states under 18). Fans 20+ per owner; this is legal safety |
| R2 | W6AX-1 / W6AX-2 (triple mass send) | no red test yet; write one |
| R3 | W6CE-1 | relay-only, per OQ-27 list |
 YAGNI rows (CVA-06, CVA-10, W5BB-8) skipped. Drift-unknown 25 = code not on head → not queued here (belongs to commercial-integration line).
Live confirm after F3 (or F4) and after F6.
