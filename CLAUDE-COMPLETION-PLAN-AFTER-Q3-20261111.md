# Twin Plan C — completion plan for Claude after independent Q3 review
Prepared for Jesse and Ronan, 11 November 2026, Brisbane.

Claude: complete the existing Twin engine and qualify reliable conversation plus the bounded one-photo/USD3 sale for Malie and the existing two test fans. Preserve the existing architecture and delivery authority. This plan is based on independently reproduced defects at c657a77, not on an assumption that the prior green selections establish completion.

## 1. Authority, scope and ownership

Jesse requested this plan and wants the bounded live test after the required checks pass. That request does not make the present FAIL candidate ready to activate.

Immediate implementation: offline on Work Server. Keep the established deployment boundary: AWS/host actions and Q4/Q5 remain with the assigned authorized deployment owner unless Jesse explicitly transfers that responsibility to Claude. Coordinate the final handoff with that owner; do not leave them an unexplained list of gates.

Frozen product goal:
- Existing creator Malie, one verified account/binding, fans 318022380 and 601164546 only.
- Autonomous ordinary replies; no routine per-message approval.
- One approved photo at USD3.00, no discounts, bundles, customs, mass sends, proactive campaigns or additional products.
- One approved DeepSeek route through OpenRouter, no fallback.
- Existing creator/fan-scoped memory, profile, event processing, model ledger, commerce and TextAuthority remain the owners.
- Text-only is an intermediate mode. Passing it must not be reported as completing the conversation-plus-sale goal.
- Initial live testing is bounded and observed by a person able to stop it. Observation does not add a per-message approval gate.
- Commercial authorization, provider acceptance, unlock/access and financial settlement remain distinct.
- No permission to message real customers outside the two approved test fans, buy credits, increase limits, launch commercially or make a purchase on Jesse's behalf.

Claude owns the offline repairs and evidence in his existing workspace. Do not launch another implementation effort or modify frozen historical checkouts. Preserve uncommitted/newer work; never reset to the named baseline blindly.

## 2. Read first and verify the starting state

Independent Q3 report and evidence:
 /srv/projects/twin-plan-c-q3-independent-c657a77-20261111/
Read START-HERE.md, REVIEW.md and HISTORICAL-MAP.md.
New independent test: test_q3_wording_boundaries.py.
Archive: TWIN-Q3-C657A77-REVIEW-20261111.zip
SHA256: b2e8aeeabc01f7623842568a51b64fe200491ac7f924fe059f813c395fc7bf60

The archive is attached. It contains exact commands, raw logs, source/image verification, the independent failing tests, truthful controls and the 36-row historical mapping. It contains synthetic examples, not live credentials or private fan transcripts.

Verified starting source:
 /srv/projects/twin-engine-completion-20261107/claude-completion-v4-20261110/integ
 Branch claude/plan-c-v4-integ-20261110
 HEAD c657a77319c3fe32dd787ae38cab6e5a0870fd39, clean, no remote
 Dashboard /srv/projects/twin-engine-completion-20261107/codex-plan-c-dashboard-20261107
 HEAD 3cedc8f5c6d3c9f493d6ef35f4162a5207b5b589, clean
 Image sha256:81e44fb2d90bb86042fc8b4006a7008ffb7d084c09bc2cf7c3723d96ab9f39c4
 Schema 0129_reconnect_receipt

Verify hostname, paths, commits, status, active jobs and actual schema before proceeding. If the source has advanced, preserve it, inspect the intervening diff and reproduce against that current state. Do not reapply already-completed repairs.

Existing independent results on the exact c657 image:
- 278 unit/ops checks passed, plus 54 subtests.
- 123 database/counterexample/journey checks passed.
- Four new wording cases failed; two truthful controls passed.
- The 34 disputed sales nodes failed exactly as before; ten additional cases passed: seven preparation/history/sale cases and three retry/receipt cases.
- One normal USD3 sale, matching unlock and continued access conversation already passes. Do not rebuild that path.

The broad builder suite ran on e35137e, not c657a77. Only two added test files differ. Keep that provenance accurate.

## 3. Repair A — product assertions introduced by the outgoing draft

Demonstrated failure:
- Signed incoming fan message "hello".
- Fake model returns "The photo costs four." OR, in a separate case, "It is a ten-minute video."
- Current engine sends the unsupported text to the fake provider.

This is a validation counterexample. It is not an observed DeepSeek conversation, and no wrong charge or media-ID bypass was demonstrated.

Cause:
 service/conversation_engine/claims.py:93 derives product_context from incoming text/referents.
 claims.py:348-357 skips new product-wording checks when that flag is false.
 engine.py:531-536 repeats the same check with the same gap.

Implement the smallest correction within this existing owner:
- Identify asserted product price/type/duration claims from the outgoing draft itself.
- Validate those assertions against the existing scoped snapshot and verified product/card terms even when the fan did not first mention a product.
- Missing evidence must not support an assertion.
- Preserve distinction between a new offer, informational answer and discussion of an existing card.
- Text-only must not acquire media, offer authority or a fabricated price snapshot.
- Full mode must retain accurate USD3/photo wording and exact frozen terms.
- Preserve benign ordinary numbers, non-product references, negation, quotations, questions and hypotheticals.
- Keep the existing single correction path, accounting and terminal handling. Add no fallback model, extra retries or parallel checker service.

Acceptance:
- Preserve the two independent failed tests and make them pass through signed intake, ModelRuntime, judge, engine, TextAuthority and fake wire.
- Unsupported text must never reach the fake provider.
- A truthful correction must still send through the existing bounded mechanism.
- Existing U1 positives/negatives, explicit currency checks, media quantities and accurate full-mode USD3/photo cases remain valid.
- Include ordinary input, relevant incoming questions and existing-card discussion so the repair does not depend on one trigger phrase.

## 4. Repair B — preserve the subject and negation of creator facts

Demonstrated synthetic profile:
 job=teacher; family=my sister is a nurse; pets=no dogs.
Current engine sends "I work as a nurse" and "I have a dog".
Truthful "I work as a teacher" and ordinary chat pass.

This was a synthetic fixture; it did not change Malie's real profile.

Cause:
 claims.py:644-670 pools words from every profile value and treats token inclusion as factual support.
 model_output.py:442-461 consumes that result.

Correct the existing grounding check:
- Ground the recognized claim against the relevant existing fact, retaining who it describes and whether it is affirmative or negative.
- Another person's occupation must not support the creator claiming that occupation.
- A denied/absent pet must not support ownership.
- Do not let an unrelated profile field or the fan's claim become an approved creator fact.
- Preserve documented paraphrases and truthful denials.
- Ambiguity must not become positive support; use existing invalid/correction/handover behavior.
- Do not create a second profile store, new profile-precedence system, broad regex framework or unapproved schema migration.

Acceptance:
- Preserve the two independent failures and the two truthful controls.
- Test relevant positive/negative facts and another-person versus creator statements.
- Keep durable profile consistency, privacy, creator boundaries, truthful identity wording and allowed public-contact behavior.
- Do not invent missing Malie facts or impose a new Telegram ban. Use the approved durable profile; the actual host profile still needs verification in Q4.

## 5. Complete the bounded sales proof using the existing path

Do this after the two shared wording repairs are joined, using the same candidate. This is completion of the approved one-photo milestone.

The 34 current sales failures cannot simply be waived:
- Their old fixtures lack the authorization/coverage now required by journey.backfill_complete.
- Forcing that gate true made 33 pass; it intentionally bypassed a safety prerequisite and is diagnostic evidence only.
- Successful normal-path examples already exist in test_plan_c_stopped_backfill_pg.py and test_plan_c_nonempty_history_pg.py.

Repair setup through the existing legitimate preparation:
- Configure the stopped scope, disabled delivery/runtime and existing budgeted preparation authorization.
- Use pilot_scope.authorize_stopped_backfill and the guarded FansBackfill fake-provider path.
- Establish actual completion/exhaustion coverage with the correct fan, account, binding, provider epoch, product and date.
- Do not force backfill_complete=True, manually insert an approved fan grant, seed a successful purchase or mark a run complete to reach the assertion.
- Keep recent-only/insufficient-history, older-purchased and binding-changed negative controls.

Run all 34 original nodes after valid preparation. Preserve their safety assertions. If any still fail, diagnose that specific normal-path failure before changing application code. Do not weaken an assertion to defend a repair.

They must prove:
- Accurate answer plus relevant authorized offer; an informational price answer alone sends no card.
- New photo request reopens a paused sale on the same turn under Jesse's existing decision. Time passing or an unrelated greeting does not itself authorize a new paid action.
- Automatic grant preparation, exact USD3 card and approved photo, one delivery.
- Corrections and proved-never-wired cancellation can produce only the permitted separate successor; old authority stays dead.
- Accepted, wire-started or uncertain delivery never gets an unsafe replacement.
- Restart around grant/decision/receipt creation and competing workers converge.
- Existing-card references, current ownership, stale media/coverage and deletion preserve accurate context and prevent duplicate sales.
- Exact matching unlock produces the correct access state; wrong fan/message/amount/media does not.
- Transaction and reversal evidence remain separate; unlock alone is not called financial settlement.
- Conversation continues appropriately after purchase or a legitimate commercial hold; unrelated fan context remains separate.

## 6. Close the remaining offline interactions

Reuse existing tests and owners. Add only meaningful missing cases:
- Combined burst + memory + corrections/clears + truncation/omission handling through the rendered prompt and final decision (missing combined N7).
- Provider-request exhaustion during a tick and daily-unit boundary, retaining reservations, holding only the appropriate work and allowing required recovery (missing N6).
- Actual authenticated dashboard component to the corrected backend: authorized stop/takeover/handback, stale and unauthorized rejection, held-work display and separate case resolution. Mocked-backend UI contracts alone do not complete this.
- Keep passing retry-after-stop, accepted receipt after crash, pre-wire retirement, unknown wire, third-fan exclusion, creator/account binding, repeated events and multi-worker cases.
- Preserve U2 log retry identity, U3 fan/global hold distinction, U4 limited internal takeover permission, U5 durable length verdict and Q1 idempotent reconciliation.

D1-D3 are product choices, not permission for a redesign:
- D1: reviewed tip replay under a fresh key currently re-holds that fan.
- D2: repeated empty OTP challenge currently re-holds; identical bytes cannot prove it is merely replay.
- D3: interrupted multipart remainder is cancelled; after resumption the request waits for new fan input.
Preserve current conservative behavior while completing independent repairs. Do not silently deduplicate challenges or resume stale parts. Bundle the exact remaining owner decisions once, with observed behavior and consequences; do not stop unrelated authorized work while waiting. Obtain Jesse's disposition before declaring these product outcomes accepted.

Keep legitimate no-contact, takeover, operator-case, unresolved-money and unknown-send holds distinct. Handback must not erase an independent case or contact restriction.

## 7. Freeze and independently qualify one release candidate

- Commit the completed changes in the existing work area; record exact backend/dashboard commits and clean/dirty status.
- Build from that commit, without source overlays. Preserve source/image hashes and schema identity.
- Run the four independent counterexamples unchanged, truthful controls, affected regression suites and joined text-plus-sale journey matrix on that final artifact.
- Refresh broad evidence affected by the changes. Identify unchanged-source evidence reuse explicitly; never label an earlier image's run as a new final-image execution.
- Triage failures by exact node and prerequisite. A runner-only ownership failure may use a documented isolated-capability variant; do not assume that qualifies production privileges.
- Do not skip the original assertion or accept a forced prerequisite as normal-path proof.
- Keep one PG test container at a time in the small shared lab. Respect its current 0.5 CPU/512 MiB configuration; do not reset shared data or leave changed limits/jobs behind.

Deliver a compact closure table: issue → exact implementing commit → old failure → final-image test/log → remaining limitation.
Independent review should challenge the focused final diff and composed evidence. It must inspect the preserved counterexamples and any altered assertions. Do not restart an unrelated architectural audit or change scope to get a pass.

The current Q3 result is FAIL. Only actual new evidence can close it. Do not report "zero issues" or commercial readiness from test counts.

## 8. Q4: prepare the real bounded trial through the authorized host owner

This work follows offline qualification and the final independent review. Current source paths, past AWS observations and old budgets are not proof of current deployed state.

The assigned host owner must:
1. Inventory every existing writer, chatter/tool, mass queue, scheduler, worker and admitted send for Malie's account. Verify durable local account fencing in every relevant process. Independently confirm external tools/staff cannot send to the pilot fans during the trial; webhook reaction alone is insufficient. Do not cancel or unsend others' queues without permission.
2. Verify current creator/fan bindings, exact photo/version/hash, current readiness, USD3 catalogue terms and required ownership coverage through authorized reads. Do not substitute cached provider evidence or guess identity from email.
3. Inspect actual approved durable profile and exact model route/upstream/prompt/config. Run approved held-out writing cases and anonymized historical/multi-turn replays against the real model with a non-sending provider. Evaluate full replies for relevance, unsupported facts, invented refusals, repetition, identity and creator limits. Scripted models cannot establish this.
4. Reconcile actual remaining budgets before any paid call. Retain approved 500 total monitored FansAPI-credit policy, accepted possible overshoot, auto-top-up off, existing early-stop/request controls, and USD5 total AI including prior/unknown reservations. Never reset counters/reservations to obtain fresh room. Read current balances/usage; old figures are historical.
5. Install one coherent receiver/sender/supervisor/guard/config/mount set inactive. Hash-pin the real guard; verify event shapes, log pagination/retry identities, URL/subscription/config binding, service permissions and watchdog behavior. Retain raw-body signature verification; expose only the intended webhook ingress.
6. Qualify the populated backup in quarantine, schema-compatible rollback, preservation of accepted/unknown evidence, actual stop/offboarding, restart, tunnel/outage detection and evidence-based reconciliation. Unknown gaps remain held.
7. Reconcile legacy ambiguous pre-fix model rows before reuse. Do not infer completeness, release costs or mandate a new migration without evidence.

Use existing secure credentials/configuration. Do not email or log secrets. No bypass of provider access denial, permission boundaries, unknown-send holds or spending rules.

Report only concrete unmet gates; continue independent authorized preparation. A missing fact/access decision should be explicit and bundled rather than repeated as vague approval requests.

## 9. Q5: controlled activation and the actual test

Activate only when the required review, offline and real-readiness evidence passes under the existing approval/ownership record.

- Enable the exact verified two-fan scope through existing controls.
- Start with ordinary conversation and observe current supervision. Verify stop/takeover before extending to a paid action; obey actual remaining units and throttle.
- When full-mode sale qualification and exact-photo binding are complete, enable only the approved USD3 photo. No additional products or commercial audience.
- Have the authorized test fan request the photo and manually make the intended test purchase. Do not purchase on Jesse's behalf without specific authority.
- Verify one exact card, matching unlock/access, accurate continued conversation and no duplicate send.
- Watch the initial bounded session, including the required healthy supervision interval, without extending counters/limits to force completion.
- Apply the existing stop/hold at the correct scope on competing writer activity, identity/binding mismatch, missing event interval, stale supervisor/credit evidence, unknown send, unexpected charge/media or breached budget boundary. Account-wide uncertainty holds the account; a fan-specific unresolved action holds the affected work while already-proven independent other-fan activity follows the existing policy. Preserve evidence and recovery protections; do not turn every fan hold into a new global stop or clear a hold just to finish.

If text-only passes first, label it as text-only. It must not hide the remaining sale work or be reported as the complete goal.

## 10. Completion package

Provide:
- Final source commits, immutable image and schema; deployed component manifest separately.
- All preserved failing cases, actual final commands/results and changed-assertion explanations.
- Historical closure table and current unresolved items.
- Actual model/provider qualification, costs/reservations, isolation proof and backup/rollback evidence.
- Trial outcome for both fans and the USD3 sale, or the precise remaining blocker.

Until checks pass, report the candidate honestly as FAIL or BLOCKED. Keep improvements in the existing engine and retain the demonstrated send/recovery protections.
