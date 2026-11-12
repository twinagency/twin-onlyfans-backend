# Operator retirement of unconfirmed Plan C text: evidence for the policy decision

Branch `claude/unknown-send-retirement-20261112`, worktree `/srv/projects/claude-unknown-retire-20261112`,
based on commercial integration `e948ee2` (port of `590e644`). Offline only. Nothing deployed, no provider or model call.

## 1. What the path does

`send_recovery.read_absence_history` then `send_recovery.release_unconfirmed_send` (library only, see limitation L1).

- Scope: ONE engine-authored, plain-text, zero-price reply of a Fans API Plan C pilot fan whose provider response was
  lost and whose intent is `unknown` (reason `invalid_receipt`, `ambiguous_transport` or `dispatch_expired`).
  Priced, media, preview, offer, quoted, receipted, projected, manual/campaign/engagement intents are refused.
- Effect: the intent becomes `cancelled` / `operator_released_unconfirmed_send`; the HOLD on the fan is lifted.
  Everything else is kept: payload, hash, token, wire marker, attempt (stays `unknown`), reservation (`used_units` never
  goes down), engine decision `held`, its remaining multipart parts `cancelled`, the request `cancelled`.
- The next fan message is planned as ordinary new work (new key, normal authority, snapshot labels the old action
  `unconfirmed`).
- This is an OPERATOR RETIREMENT of unconfirmed work. It is not a finding that the original send did not happen.
- Late positive evidence: `record_late_positive_evidence` (new) keeps a later exact positive observation beside the
  retirement. It never accepts, replays, refunds or reopens anything; the ordinary `reconcile` still refuses the
  retired engine intent.

## 2. The exact policy being asked for approval

| item | value in code | tunable |
|---|---|---|
| quiet period | 30 min (`QUIET_SECONDS=1800`), measured from the LAST moment the attempt could be on the wire (max of wire start, dispatch, attempt update) | caller may only lengthen it (<= 7 days) |
| history freshness | read must END <= 2 min before the command (`FRESH_READ`) and must START after the quiet period ended | fixed |
| coverage | guarded read, newest first, <= 10 pages of 100; complete only when the provider reports the chat start, or a message older than dispatch minus 60 s is seen | fixed |
| ambiguity window | any non-fan message within 5 min after dispatch, any later creator message with the same or near-same text (>= 0.85), any canonical outbound message, unreadable/duplicate/future-dated rows | fixed |
| newer request | a live (signed webhook, not import), current, not-deleted fan message after the send, not the one being answered, named by the operator | required |
| actor | existing pilot `reconcile` permission, fan inside the pilot scope, audit key + current control revision | required |

## 3. What an observed absence does and does not prove

Does: within the page limit, the provider's own listing of this chat, read after 30 quiet minutes and no more than
2 minutes before the command, showed no message that could be ours and the fan has written again.

Does NOT: prove non-delivery. 30/2 alone cannot. A message can be deleted or unsent by anyone, omitted by the provider,
or delivered later than the read. The record says so (`limitations` in the audit row and attempt evidence), the
attempt stays `unknown`, the reservation stays spent, and the operator summary says "a decision, not proof it never
arrived". A message that surfaces later is kept (bullet 7) and flags the conversation for attention.

## 4. Per bullet result

Tests: R = `service/tests/test_plan_c_unknown_send_release_pg.py` (existing), E = `..._unknown_send_evidence_pg.py`
(existing), N = `..._unknown_send_retirement_pg.py` (new, 49 tests).

| # | bullet | result | tests |
|---|---|---|---|
| 1 | exact account/binding/fan/intent, payload, wire evidence preserved; plain zero-price text scope only | PASS | N `test_every_identity_payload_and_wire_field_of_the_retired_intent_is_preserved` (only status/reason/updated_at change); N `test_only_a_plain_unchanged_unknown_text_of_the_fan_can_be_retired` (20 shapes incl. wrong binding, edited body/hash, receipt, provider id, quote, offer action, not-unknown decision/attempt, other transport); N `test_paid_or_media_shapes_...` (schema forbids; with the check dropped on a throwaway DB the Python predicate refuses too); N `test_the_retirement_needs_a_pilot_scope_that_contains_the_fan`; R `test_the_intent_must_be_the_unchanged_unknown_plain_text_send_of_that_fan` |
| 2 | one page missing text, stale/incomplete/unreadable/wrong-account history insufficient; coverage, pagination, timestamps, limits established | PASS (L2, L3) | N `test_a_matching_message_on_an_older_page_blocks_...`; N `test_history_that_is_unavailable_disordered_...`; N `test_a_history_that_stopped_before_it_reached_the_send_is_incomplete_and_says_so`; N `test_the_audit_record_states_the_observation_coverage_pagination_timestamps_and_limits`; N `test_reaching_the_chat_start_is_recorded_as_such`; N `test_a_hand_built_or_edited_history_observation_is_refused_...`; R `test_stale_history_is_refused`, `test_incomplete_or_truncated_history_is_refused`, `test_history_read_before_the_quiet_period_ended_does_not_count`, `test_history_for_another_account_fan_or_creator_is_not_accepted`, `test_history_read_needs_the_current_pilot_request_guard`, `test_history_covers_the_send_when_an_older_message_is_seen_on_a_later_page` |
| 3 | unknown outcome and reserved units retained; never not_sent; no refund for lack of a match | PASS | N `test_no_record_ever_says_not_sent_and_no_unit_is_refunded_...`; R first test (`used` unchanged, attempt `unknown`, reservation kept, +1 only for the fresh reply); R `test_a_message_that_could_be_ours_in_history_refuses_...` |
| 4 | intended actor/permissions, current revision, audit/idempotency key, current newer fan request | PASS | R `test_the_actor_must_hold_the_reconcile_permission_and_the_command_must_be_exact`, `test_replay_of_the_same_command_key_is_idempotent_...`, `test_a_fresh_fan_message_after_the_send_must_exist_and_be_the_one_named`, `test_without_any_new_fan_message_the_fan_stays_held`; N `test_a_control_change_after_the_operator_looked_makes_the_command_stale`; N `test_the_newer_request_must_be_a_live_current_fan_message_not_deleted_or_imported`; N `test_the_actor_is_recorded_with_the_reason_and_the_key_...` |
| 5 | retired intent/key and remaining multipart parts never replayed; new work normal and carries uncertainty | PASS | N `test_remaining_multipart_parts_are_cancelled_and_never_sent_after_the_retirement` (parts 2 and 3 of the retired sequence never reach the wire, all keys distinct, fresh plan carries "unconfirmed"); R `test_a_released_unconfirmed_send_is_never_replayed_or_reopened_by_the_engine`; R first test (fresh key, snapshot says unconfirmed) |
| 6 | stop/takeover and other unresolved evidence still win | PASS | R `test_stop_takeover_and_other_holds_still_win` (7 cases), `test_a_pending_human_message_is_pilot_evidence_that_still_holds`; N `test_a_stop_after_the_retirement_still_blocks_the_new_work` (no_contact, paused), `test_a_takeover_after_the_retirement_still_blocks_the_new_work`, `test_a_scope_stop_after_the_retirement_blocks_the_new_work`; N `test_the_retirement_frees_only_its_own_fan`; R `test_two_fans_are_released_independently_...` |
| 7 | late positive evidence retained/reconciled without reopening authority or erasing the conflict | PASS after fix (was FAIL: the only history route refused the retired intent, so the evidence was not retained) | N `test_a_late_signed_sent_event_for_the_retired_text_is_kept_as_evidence_and_reopens_nothing`; N `test_late_positive_provider_history_is_recorded_against_the_retired_intent_without_settling_it`; N `test_late_evidence_must_match_the_retired_payload_exactly` (text, fan, price, time); N `test_late_evidence_needs_the_actor_the_released_state_and_complete_history` |

Defects found and fixed with a failing test first (commit `946b1df` red, `91fe160` fix):
1. Bullet 2: the audit record had no coverage, freshness policy or limitations. Added.
2. Bullet 7: no way to retain a late positive observation of a retired engine intent. `record_late_positive_evidence`.
3. Wording: docstrings, comments and the operator summary said history "proves" absence. Reworded.
4. Provenance: `release_unconfirmed_send` accepted any hand-built `HistoryRead`. It now refuses an observation the
   reader did not issue in the same process (fail closed).

## 5. Limitations (path stays unavailable for live use until these are decided)

- L1. No operator API, CLI or dashboard route calls `release_unconfirmed_send` or `record_late_positive_evidence`.
  Today the path cannot be used live. Wiring is separate work and must call read and release in one process (L3).
- L2. A clean history is not proof (section 3). Whether 30 min / 2 min is enough risk tolerance for a possible
  duplicate to the fan is a business decision.
- L3. Observation provenance is a process-local fingerprint, not a signature. Read then release in the same process
  only; a cross-process flow needs a stored, signed observation (not built).
- L4. The test rig's account id (`acc-a`) is not OnlyFans-form, so the native matcher cannot parse it. Existing
  `reconcile` tests stub the matcher. The new late-evidence tests keep the real matcher and give only the account
  parser its production form. The real Plan C account id format has not been run through `reconcile` or the new function.
- L5. A late signed `messages.sent` for the retired text is retained as outside-writer evidence (held, later handover),
  not linked to the retired intent by message id. It fails closed (the fan is held) but misattributes our own message.
- L6. Only plain text is covered. A paid/media unknown send has no retirement path and stays held.

## 6. Decision needed from Jesse / Ronan

Approve or reject, for Plan C plain-text unknown sends only:
1. 30 minutes quiet (lengthen only) and 2 minutes freshness, with the wording in section 3 ("operator retirement,
   not proof of non-delivery");
2. accepting that the fan may receive a duplicate if the original message was only delayed or hidden;
3. whether to wire an operator route (L1) and, if so, whether read+release must be one process (L3) or needs signed
   observations.

Until then the path stays library-only and unavailable for live use.

## 7. Test runs (image `twin-engine-offline-main:20261107-r6`, worktree mounted read-only, logs in `/srv/projects/claude-a2-logs-20261112/`)

Source under test: branch `claude/unknown-send-retirement-20261112`, HEAD `91fe160` plus this evidence file.

| suite | result | log |
|---|---|---|
| baseline (existing release + evidence suites at e948ee2) | 69 passed | `baseline-unknown.log` |
| new retirement tests, red at `946b1df` | 13 failed / 34 passed (then 9 failed / 5 passed after test fixes, all 9 red for the missing feature) | `red1-retirement.log`, `red2-retirement.log` |
| unknown-send: release + evidence + retirement | 118 passed | `final-unknown-send.log` |
| retry-due + text delivery | 63 passed | `final-retry-due-text-delivery.log` |
| recovery (send, general, native pilot, auth, crash, fairness) | 72 passed, 10 errors: all `test_onlyfans_recovery_pg.py` backup/restore tests, the image has no `pg_dump` | `final-recovery.log` |
| release package, contextual release, events stage 3, multipart | 88 passed, 5 failed, 3 errors (see below) | `final-release.log` |
| events stage 3 rerun with `ops/plan-c-supervision` on PYTHONPATH | 86 passed (the 3 stage-3 failures were a missing module path) | `final-stage3-counters.log` |
| contextual release on the UNCHANGED base e948ee2 | same 1 failure as on this branch | `base-contextual-rollback.log` |

Not green, not caused by this change: 3 release-package errors and 10 recovery errors need `pg_dump`; 1 release-package
failure needs `ONLYFANS_COMPATIBLE_ROLLBACK_DIR`; `test_mode_save_preserves_scope_and_populated_rollback_retains_unknown`
fails on the base too (the 0136 -> 0135 downgrade reaches the Moxy owner migration, whose `downgrade()` raises).
These were not rerun with the missing tools, so release/recovery backup-restore is NOT claimed green.

Changed existing assertions: none. Changed existing text: docstring of `test_plan_c_unknown_send_release_pg.py` (reworded,
no assertion touched) and the operator `unknown_send` summary string (still contains `audited release`, which the
existing test asserts).
