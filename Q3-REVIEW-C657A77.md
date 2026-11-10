# Twin Plan C — independent Q3 review

Reviewed 11 November 2026, Brisbane. Tests completed 10 November 14:27 UTC / 11 November 00:27 Brisbane.

**Offline two-fan text-only candidate: FAIL.** Two factual-validation defects were reproduced through the normal signed-intake → model → judge → engine → authority → simulated-send path.

**Combined conversation plus one-photo/USD3 goal: FAIL.** The same defects affect its conversation requirement. Completion also remains **BLOCKED by missing sales regression and integration proof**, described below. This is not a finding that the actual price/media authorization can be bypassed.

Several important repairs pass independent checks. “All five areas repaired” and “zero open product defects” are too broad. This review does not authorize or perform repairs, Q4, Q5 or activation.

## 1. Exact state and evidence identity

- Backend: /srv/projects/twin-engine-completion-20261107/claude-completion-v4-20261110/integ
- Branch: claude/plan-c-v4-integ-20261110
- HEAD: c657a77319c3fe32dd787ae38cab6e5a0870fd39. Clean before and after testing; no remote.
- Comparison baseline: 6f1f64880f06afe0fcada1d7175a6b62b5739f3f.
- Dashboard: /srv/projects/twin-engine-completion-20261107/codex-plan-c-dashboard-20261107, branch codex/plan-c-dashboard-20261107, HEAD 3cedc8f5c6d3c9f493d6ef35f4162a5207b5b589. Clean; unchanged.
- Final image: sha256:81e44fb2d90bb86042fc8b4006a7008ffb7d084c09bc2cf7c3723d96ab9f39c4.
- Source archive: candidate-c657a77319c3/source.tar, SHA256 eade49b46a2d020cfbaec677085c045aae203e217f8c00b39f8574a4162a878f.
- Independently compared git archive HEAD, that archive, and all 2,639 regular archive files in /app in the final image: no mismatch.
- Schema: 0129_reconnect_receipt; no migration in this change.
- Saved guard copy: ops/plan-c-supervision/host-guard-5aef81d2/guard_core.py, SHA256 5aef81d23d14bf8518ddc1028ccd5d32c9c1084015626a300732dd6ca03d9617. This verifies the saved file, not the current AWS installation.
- Six supplied evidence attachments match their Work Server counterparts by SHA256. The handoff is a claims document; the mid-run closure/journey tables remain superseded by its later entries.
- c657a77 differs from e35137e by exactly two added test files: test_plan_c_q2_n4_operator_http_pg.py and test_plan_c_q2_n5_third_fan_text_only_pg.py. No application file differs.

Reviewer evidence root:
 /srv/projects/twin-plan-c-q3-independent-c657a77-20261111/

Builder evidence root:
 /srv/projects/twin-engine-completion-20261107/claude-completion-v4-20261110/evidence/

References below to service/, ops/ or docs/ are relative to the exact backend path above.

## 2. Two confirmed in-scope defects

### Q3-1 — unsolicited false product wording still gets sent

**Reproduced, existing U1 accuracy requirement not fully repaired.**

Incoming “hello”, scripted valid model reply “The photo costs four.”: one model call, one accepted simulated text send. The same happened with “It is a ten-minute video.” No card, media or monetary charge was sent.

The new product-wording checks run only when grounding.product_context is true. That flag is derived from incoming text and existing referents. With an ordinary greeting and no offer context, the checks return before examining the model's newly introduced product assertion.

Exact code:
- service/conversation_engine/claims.py:93 — incoming/referent-derived product_context.
- claims.py:348–357 — early return before bare-price, media-type and duration checks.
- claims.py:475–484 — explicit currency checking remains; bare “four” is not caught there.
- service/conversation_engine/engine.py:531–536 — final engine uses the same grounding/check; it does not close the gap.
- Existing test_plan_c_final_counterexamples.py:37–66 always supplies “How much is the photo?”, so its passing result does not cover the ordinary-greeting case.

Evidence:
- independent-wording.log, test_q3_wording_boundaries.py:
  test_unsupported_assertion_is_corrected_before_dispatch[unsolicited-price]
  test_unsupported_assertion_is_corrected_before_dispatch[unsolicited-media]
- Both fail because the unsupported text actually appears in the mock provider's outgoing posts.
- The existing U1 counterexamples and two truthful controls pass.

Effect: the bot can introduce an inaccurate price or describe an unavailable video while ordinary chatting. Frequency with the actual model remains unmeasured.

Smallest sensible repair: examine product assertions introduced by the draft itself, even when the fan did not first ask about a product. Ground those assertions in the existing scoped terms/media evidence. Preserve ordinary non-product numbers, quotes, questions, hypotheticals, negation and the current bounded correction path. Do not turn this into a blanket ban on numbers, change actual card terms, or add another conversation engine.

Acceptance: preserve these two failing signed-path tests, existing U1 positive/negative cases, truthful ordinary replies, and the full-mode accurate USD3/photo path. Assert unsupported text never reaches the provider and an eligible correction can still send.

### Q3-2 — another person's facts and negative facts can authorize false creator claims

**Reproduced, U1b grounding is incomplete.**

Synthetic approved profile, installed through normal durable configuration:
 job = teacher
 family = my sister is a nurse
 pets = no dogs

Incoming “What do you do?” / model “I work as a nurse.” was sent.
Incoming “Do you have a dog?” / model “I have a dog.” was sent.
“I work as a teacher.” and an ordinary greeting both sent correctly.

Exact code:
- claims.py:644–648 pools the words from all profile values.
- claims.py:666 accepts a recognized claim when its words are a subset of that pooled set.
- service/conversation_engine/model_output.py:442–461 uses this check in creator validation.

This loses who a fact describes and whether it is negated. It is more specific than the disclosed limitation that the vocabulary is finite. The supported vocabulary itself is incorrectly grounded in these cases.

Evidence:
- independent-wording.log / test_q3_wording_boundaries.py:
  [other-person-job] and [negated-pet-fact].
- Both traverse the ordinary pilot path and reach the simulated sender.
- These are synthetic profiles; I did not inspect Malie's live profile or claim it contains these exact values. No cross-creator data leak was demonstrated.

Smallest sensible repair: use the existing profile's relevant fact and preserve subject and negation when validating a claim. A sister's occupation must not support the creator's occupation; “no dogs” must not support ownership. Do not create a second profile store or weaken durable profile binding. Ambiguous evidence must not become positive support.

Acceptance: these two failures, correctly documented own job/pet facts, negated and other-person facts, supported paraphrases, and existing creator correction/boundary tests through the same normal path.

## 3. Repair-by-repair assessment

| Repair | Independent assessment | Evidence and limit |
|---|---|---|
| U1 product wording | PARTIAL; Q3-1 confirmed | Existing matrix and pipeline checks pass; newly introduced assertions after ordinary input escape. |
| U1b creator facts | PARTIAL; Q3-2 confirmed | Existing unit/PG cases pass; pooled-word grounding does not preserve subject/polarity. |
| U2 delivery-log retry identity | Verified for tested offline cases | delivery_history.py:236–300 now binds key, event, destination and payload hash; collector/interval validation reuse it. Different-payload success cannot resolve the tested failed event. Whole ops suite passes on final image with stated file-ownership capabilities. Real provider retry shape remains Q4. |
| U3 fan hold versus global readiness | Verified for tested offline cases | pilot_scope.digest_hold_scope and plan_c_worker readiness distinguish account and fan holds. test_plan_c_u3_readiness_pg.py uses the pinned guard and real supervisor logic: other fan progresses; global/credit/identity/stale/stop conditions still deny. External I/O is mocked. |
| U4 automatic handover | Verified for tested offline cases | store.py:410–446 permits engine-journey only for request_takeover outside HTTP agency scope. Eight tests cover permission denial, concurrent/repeated handover, fan isolation and handback. No activation/resume power was added. |
| U5 truncated-response crash replay | Verified for new rows in tested cases | model_ledger.py:254–321 persists length verdict with response/settlement and rejects promotion to valid output. Final tests include crash boundaries, concurrent workers, unknown-cost reservations, one regeneration and process-kill recovery. Pre-fix ambiguous rows need a cutover disposition, not an assumption that they were repaired. |
| Q1 reconcile race | Verified for tested interleaving | reconciliation.py:123–130 rechecks the exact command replay after the unlocked target refuses. Deterministic regression and the repeated concurrency tests pass; one settlement, no send permission. |

The changes remain in the existing profile, model ledger, event/readiness, store-permission and monitor owners. I found no evidence in the inspected diff of a replacement engine, a second profile store, removal of delivery authority, or a new sales strategy. That architectural observation does not erase the reproduced wording failures.

## 4. What I actually ran

Every run below used the final image, with no application source overlay. Provider/model transports were mocks. PG runs joined the existing lab container's network namespace, whose network mode is none. One PG test container ran at a time.

| Fresh run | Actual result | Evidence |
|---|---|---|
| U1/U1b unit matrices + entire ops supervision suite | 278 passed; 54 subtests; exit 0 | pure.log, pure-command.json, pure-result.json |
| Final original counterexamples, U3/U4/U5, U1b PG, canonical reconciliation and N1–N5 journeys | 123 passed; exit 0 | pg.log, pg-command.json, pg-result.json |
| Six independent wording cases | 4 failed, 2 passed; exit 1 | independent-wording.log, test_q3_wording_boundaries.py |
| Exactly the 34 disputed sales nodes, seven normal preparation/history/sale cases and three retained retry/receipt cases | 34 failed, 10 passed; exit 1 | sales-and-retained-delivery.log and command/result JSON |

The 34 freshly failing node IDs exactly match the builder's 34 sales failures: none added or missing. The four independent failures are additional counterexamples, outside that existing failure list.

Fresh positive sales evidence:
- test_plan_c_stopped_backfill_pg.py::test_real_scoped_backfill_then_normal_photo_journey — unowned, recent-only, older-purchased (3 cases).
- test_plan_c_nonempty_history_pg.py — four cases, including overlapping history → a single USD3 card with exact media → matching unlock → continued access conversation; other fan's context/reply kept separate.
- test_plan_c_v3_retry_authority_pg.py — takeover and contact-stop prevent a further POST after proved-not-sent (2 cases).
- test_plan_c_v3_receipt_restart_pg.py — accepted receipt materializes after restart without resending (1 case).

The successful sales journey uses actual stopped-backfill authorization, guarded fake-provider pagination, normal intake/planning, automatic grant creation and delivery. It does not secretly seed the per-fan grant. It is legitimate evidence that a bounded sale path exists. It does not replace the remaining failure-specific sales checks.

Runner qualifications:
- Ops tests need root inside the isolated container plus CHOWN, FOWNER, DAC_OVERRIDE, SETUID and SETGID. Image filesystem and dependencies remained read-only; network none.
- PG tests ran as UID1000 with capabilities dropped.
- Lab database stayed at 0.5 CPU and 512 MiB. I did not restart it, change its limits or reset existing data. Test fixtures created and removed their own uniquely named disposable databases.
- End-state checks: both source trees clean; no review test containers still running.

## 5. Which older results I inspected, and what their claims mean

I read the raw final-e35137e0b269/new.log, affA.log, affB.log and extra.log, the final-c657 logs, Q1 diagnosis/fix evidence, forced-gate probe source/log, failure lists, before/after diffs, V3 baseline evidence and the 36-row historical register.

The broad runs were on e35137e / image c54d208d..., not c657a77:
- 586 passed.
- 865 passed / 22 failed.
- 336 passed / 12 failed.
- 191 passed / 18 failed.
These are overlapping selections, not a count of unique project tests.

Reusing that evidence for unchanged application source is reasonable because the only later changes are N4/N5 tests. Calling it a full-suite execution on c657a77 is inaccurate. This review also did not rerun every broad suite.

“Zero new failures” is supported only for the compared selected failure sets:
- 238a6a9 diagnosis: 67 failures.
- e35137e: 52; exactly 15 removed, no new IDs in that comparison.
- Earlier 6f/28f results use different subsets/revisions. They cannot be collapsed into one identical full-suite baseline.
- New independent counterexamples disprove “zero open product defects”; lack of added IDs in an old suite cannot prove that assertion.

### The 18 runner failures

The raw failures are file-ownership operations under the unprivileged runner. The complete ops tests pass independently on c657 with the capabilities specified above. I agree they should not be counted as 18 demonstrated product defects.

This does not verify the actual deployed user, capabilities, mounts, service/watchdog settings or guard. Those remain Q4.

### The 34 sales failures

The diagnosis is credible and reproduced as a failure set. The fixtures use plain history completion where the actual pilot now requires an audited, stopped-backfill receipt and sufficient coverage.

Relevant normal gate: service/conversation_engine/journey.py:280–330. It binds completed/exhausted history to account, fan, runtime binding, connection epoch, provider, approved product and preparation authorization. A recent-only window is insufficient.

The diagnostic probe in evidence/q1-diagnosis/probe/probe_sales_whatif_backfill_gate.py:11–13 forces backfill_complete=True. Its saved result is 33 passes and one failure that explicitly expects the gate to remain closed. This isolates the prerequisite as a major cause; it deliberately bypasses a protection and does not qualify the normal sales flow.

Still lacking a clean normal-path run in these 34 cases:
- answer a price question without sending a card;
- refusal → renewed interest, including a new photo request on the same turn;
- corrected queued offer and safely issued successor after proved-unsent cancellation;
- no successor after wire-started, accepted or unknown outcomes;
- restart around grant/decision creation and two-worker preparation;
- ambiguous reference to an existing card;
- stale media/expired coverage, unrelated catalogue changes;
- deletion of an accepted card without duplicate delivery.

Smallest completion: repair these fixtures to use existing stopped preparation and actual guarded backfill. Preserve their assertions and the insufficient-history negatives. Do not force the gate or seed the outcome. Investigate any remaining failure after legitimate preparation.

Text-only passing controls support separation from commerce; they do not prove that every shared path is unaffected under every interaction.

## 6. Changed tests: protection retained or weakened?

I inspected the modifications, not just the final counts.

- U5 restart assertion changes identity_verified to length_limited because the old state represented the bug. The decisive assertions remain: incomplete text never sent; correct response after restart; exactly two model calls. Fresh final-image run passes.
- U2's “successful retry” fixture now carries the failed event's body. That is required by the corrected identity contract; different-payload negative tests remain.
- U1b “two cats” example is now rejected separately; the non-pet positive example is replaced with coffee. The barista example now has an explicitly documented job. These changes are legitimate but did not cover Q3-2.
- Hold matrix fixtures enable media so that the intended hold policy is reached. Original hold assertions remain. A new test separately pins refusal when media is disabled.
- The migration test now verifies real 0104 → 0105 → current-head upgrade without invoking a deliberately forbidden destructive downgrade. This proves that upgrade path, not production rollback.
- The unmapped-delete case moved from a digest assertion to the actual held projection, retaining the no-send assertion.

I did not find a weakened safety assertion among these disclosed edits. The held/upgrade test results were inspected in builder logs, not independently rerun in this Q3 block. The newly discovered gaps concern missing cases, not a claim that these fixture changes faked success.

## 7. End-to-end journey and remaining combined-goal evidence

| Step | Current owner/protection | Q3 result |
|---|---|---|
| Receive and authenticate | FansAPI intake, retained raw evidence, event classification and projection | Signed local HTTP journeys pass; real provider shapes remain Q4. |
| Context/profile/memory | real_worker, engine snapshot, writer_context, durable scoped profile | Selected tests cover bursts/truncation and isolation; actual deployed profile not inspected. Q3-2 defeats one factual grounding check. |
| Answer and sales decision | ModelRuntime + judge; journey/sales; engine rechecks | Normal positive path exists; Q3-1/Q3-2 confirmed. Real writing quality not established by scripted models. |
| Prepare $3 offer | commerce.prepare_pilot_offer and verified_terms; stopped backfill authorization and ownership evidence | Unowned/older-purchased/recent-only controls pass. 34 related normal-path regressions remain red. |
| Send once | engine delivery → TextAuthority; frozen body/media/price, one-use authority, current permissions, wire marker and idempotency | Retry/stop and accepted-receipt recovery pass. No demonstrated amount or media-ID bypass. |
| Recognize unlock/access | events._unlocked and commerce._record_access; exact fan/message/intent/binding/grant/media/amount | Matching unlock + continued conversation passes. Unlock/access is not automatically financial settlement; transaction/reversal evidence remains separate. |
| Interrupt/recover | current event holds, per-fan controls, sequence cancellation, ledger, accepted-receipt repair and supervisor | Selected N1–N3/U2–U5 cases pass. D1–D3 and legacy ambiguous rows remain as below. |
| Dashboard/control | operator_v2 permissions/revision checks; existing dashboard | N4 real backend HTTP route tests pass. No actual dashboard-to-this-backend journey. |
| Real operation | installed receiver/sender/guard, existing bots/queues, budgets, provider/model, restore/rollback | Not contacted or qualified here; Q4/Q5 remain separate. |

Historical coverage: HISTORICAL-MAP.md maps all 36 older register entries to current paths, relevant tests and the strength of current evidence. Historical “passed” labels are not silently promoted to current closure.

### D1–D3 remain owner decisions

- D1: replaying an already-reviewed tip under a fresh key re-holds that fan; other fan can progress. N1 characterizes this. Whether an exact reviewed tip/body in the same epoch can reuse classification is unresolved.
- D2: a repeated empty OTP challenge re-holds after release. Identical bytes alone cannot distinguish a replay from a genuinely new challenge. Do not silently deduplicate it to get a green journey.
- D3: stopping after multipart part one cancels the stale remainder; after resumption, the open request does not automatically re-plan until new fan input. N3 proves this behavior, not its product acceptability.
- Manual handback does not erase an independent operator case or no-contact/evidence hold. U4/N4 demonstrate authorized case resolution and a new message can progress. Keep these separate protections.

Other proof gaps:
- Legacy pre-fix identity_verified model rows cannot be classified from that value alone. Inventory/disposition is needed before reuse; this review does not justify an automatic schema change or budget release.
- Journey B has component coverage, not the missing combined burst/memory/truncation test.
- Journey G still lacks provider-cap exhaustion mid-tick and the daily-unit boundary test.
- Journey H lacks the actual dashboard/backend run on this candidate.
- Journey J's receiver restart is in-process; an OS/service restart and host composition belong to Q4.
- Real model/profile held-out cases, current bindings, provider semantics, actual writer/queue isolation, current host files, backup/rollback and financial retention completeness were not verified.
- No finding here establishes that every possible future defect has been excluded.

## 8. Minimal ordered next work

1. Preserve this candidate/evidence and correct Q3-1/Q3-2 within the existing checks. Use the four preserved failing normal-path cases, truthful controls and existing counterexamples; retain correction limits, model reservations and all delivery protections.
2. Restore the 34 sales regressions through real stopped-backfill preparation, without bypasses. Complete missing composed offline cases and the actual dashboard-to-backend journey; obtain Jesse's D1–D3 decisions before claiming those behaviors meet the product contract.
3. Freeze one corrected image and re-run the affected/joined acceptance set, including normal $3 sale → exact unlock → continued conversation plus stop, takeover, correction, ownership, duplicate, concurrent-worker and crash/unknown cases. Record any intentionally reused evidence by exact source identity.
4. Obtain independent sign-off on that exact candidate. Only then proceed to separately scoped Q4 verification and Q5 authorization/activation. This review performs neither.

No code, deployment, permissions, live data or service configuration was changed. No other chat/agent was tasked. No AWS, provider/model call, fan message or purchase occurred.
