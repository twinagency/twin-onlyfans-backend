# Step 6 (Q1-Q13) handoff, base 48e007a, branch claude/commercial-step6-matrix-20261112

Offline only. Nothing pushed or deployed. Logs: /srv/projects/claude-c1-logs-20261112/ (run.sh = harness, summary.txt = run index).

## Combined first run on 48e007a (Step 4 + Step 5 + R0)
- Step 4 (normal_worker, sale_rules, unlock, composed): 28 passed, 0 failed (combo_step4.log). All 7 unlock tests green together.
- Step 5 (coexistence_scope_contract, coexistence_staff, voluntary_tips, tip_evidence_kind_migration): 77 passed (combo_step5.log).
- R0 + retry lineage (commercial_retry_due_lineage, r0_operator_timing, r0_red_green, r0_acceptance): 64 passed, 1 failed (combo_r0.log).
  Failure = test_mixed_versions_fail_closed_and_rollback_never_drops_retry_evidence: needs /opt/r0-baseline image. Known environment-only, not chased.

## Matrix (NOT-RUN = test written, never executed to a verdict)
| Q | Existing tests that prove it (mapped, passed in combo run) | New tests (this branch) | Status | Log |
|---|---|---|---|---|
| Q1 | test_commercial_composed_pg (answer, card, thanks) | matrix_pg::test_q1_ordinary_replies... | NEW FAIL (test, cause not confirmed) | m1.log |
| Q2 | normal_worker, sale_rules::test_specific_photo_video_and_bundle..., composed | none needed | PASS (mapped) | combo_step4.log |
| Q3 | sale_rules (writer cannot state amount, human edit, one active offer, second card refused), composed discount variant | matrix_pg::test_q3_* (7 word-form price / false media cases x model path and human-edit path) | NOT-RUN | |
| Q4 | catalogue_terms_pg (d_ owned/unknown), history_ownership_pg, sale_rules::incomplete_history | matrix_pg::test_q4_... (old purchase, partly owned bundle, unknown previews, new asset without terms) | NOT-RUN | |
| Q5 | commercial_unlock_pg (all 7) | none needed | PASS (mapped) | combo_step4.log |
| Q6 | sale_rules (decline/renew, price change, content change, newer message, revoke) | matrix_pg::test_q6_* (ambiguity; category moved, discount added/removed, terms withdrawn, media removed, previews changed) | NOT-RUN | |
| Q7 | plan_c_supervised::parallel_workers, model_retry_due::two_concurrent_workers (trial path) | matrix_pg::test_q7_... (commercial: concurrent duplicate webhook, redelivery, racing schedulers, racing unlock) | NOT-RUN | |
| Q8 | plan_c_r0_acceptance, model_retry_due, r0_red_green (trial path) | matrix_pg::test_q8_x3 (429 recovery with restart, correction+429, permanent 401) on commercial | NOT-RUN | |
| Q9 | v3_stage2_prewire/receipt/retry, commercial_unlock::accepted_but_unprojected, sale_rules::unknown_send | matrix_pg::test_q9_x3 (unknown card across restarts, never-wired card crash, unknown model call) | NOT-RUN | |
| Q10 | plan_c_s6_n6_exhaustion, v3_stage2_multipart, coexistence_staff (trial/text-only) | matrix_pg::test_q10_x3 (multipart capacity, account cap, provider budget at final wire on commercial) | NOT-RUN | |
| Q11 | voluntary_tips_pg (13), coexistence tips | matrix_pg::test_q11_... (tip beside a sale) | NOT-RUN | |
| Q12 | commercial_scope_isolation_pg (4 accounts, intake/control), history_import ten-account import, history_measurement | test_commercial_ten_creators_pg.py (10 creators, identical ids, 5 degraded modes, real sale path) | NOT-RUN | |
| Q13 | history_import, history_coverage, history_ownership, commercial_vault_pg, history_measurement | test_commercial_import_to_sale_pg.py (per-fan coverage decides the sale, gap review, overlap, readiness not complete) | NOT-RUN | |
Q14/Q15 are owned by another agent. Finding: the import stops once a scope is launched (prepare_one returns "stopped" unless status is configured), so Q13 import resume is a preparation-phase property.

## Failing test and cause
matrix_pg.py::test_q1_ordinary_replies... line 169: assert all(r.status == "completed" for r in requests_of(world, FAN_A)). Got [completed, open, open, completed, ...]. The two "open" requests are most likely the superseded drafts of the 3-message burst (older messages retired by the newer one). That is probably a wrong test assumption, not a defect, but this was NOT verified. Run was -x, so everything after it in test_commercial_matrix_pg.py never executed.

## Changed fixtures / assertions
service/tests/commercial_worker_fixture.py, additive only: launch(limits=None), build_world(folder=None, history=None). No existing assertion changed. No source (non-test) code changed, no defect fixed.

## Next exact steps
1. Fix Q1 line 169 (ignore superseded burst requests, or assert only the last request per fan completed), rerun without -x:
   /srv/projects/claude-c1-logs-20261112/run.sh /srv/projects/claude-step6-matrix-20261112 m2 tests/test_commercial_matrix_pg.py
   (needs a postgres:16 container named claude-c1-pg, POSTGRES_HOST_AUTH_METHOD=trust, removed at wrap-up).
2. Run test_commercial_ten_creators_pg.py and test_commercial_import_to_sale_pg.py. Expect test errors in guessed fixtures first (operator_v2.unresolved item keys, ten-creator ids 7002/7003, row-gap dismiss after launch, takeover/emergency stop calls).
3. Any red test that is a real defect: commit red test, then minimal fix, then explain effect. Still unexamined: category-folder move and discount add/remove between plan and send (Q6), account-cap refusal leaving the grant unspent (Q10), a product approved after the catalogue snapshot is offered without a new snapshot (exploratory, a possible open policy question, not asserted).
4. Run affected suites in shards on HEAD and on base 48e007a and compare fail sets: test_commercial_*, coexistence/tips, text_delivery, payment/unlock, operator, tests/test_plan_c_* (use run.sh, one container at a time, --cpus 1 --memory 1g). Known env-only failures: test_commercial_full_path, test_commercial_progress (order-dependent), test_history_capacity_turns, test_onlyfans_message_media, test_plan_c_r0_acceptance_pg (needs /opt/r0-baseline).
5. Update this matrix to PASS/FAIL with logs, commit.
