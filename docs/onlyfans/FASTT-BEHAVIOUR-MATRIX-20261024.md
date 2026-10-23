# FASTT behaviour matrix (handoff section 5), 2026-10-24

Compares decisions and side effects, not wording. Same model, persona and saved
settings for both columns. "Original" = `automations.ai_chatter.run` and the pure
helpers it calls; "Current native" = base dbb211b before this change; "Restored"
= this change (`automations/turn_decision.py` shared by both callers; sales basis
in `onlyfans_runtime/native_sales.py`, planner `native-text.v5`).

The pilot adapter and TEST-NOTES.txt were not available on this machine; the
"original" column is derived from source, which is authoritative. The pilot
disabled many background jobs, so it is not evidence for any row.

| # | Case | Original FASTT decision | Current native restriction | Restored result |
|---|------|-------------------------|----------------------------|-----------------|
| 1 | Direct content request | `_language.is_content_ask` AND `sell.close` -> `content_ask` -> `_turn_kind` returns `_TURN_CONTENT_ASK`; `SellLane.is_ask`; `sell_lane.qualify_snapshot` (an explicit pull lifts `_PULL_LIFTS`); priced pack attached | `native_sales.eligible` requires `is_ask` (sale possible) but `_native_messages` hardcodes `content_ask=False`, so the turn gets no content-ask beat; `selected` needs a quote from the latest inbound | `decide_turn` returns kind `content_ask`; basis `direct_request` (quote, source message); one reply plus one card through TextAuthority |
| 2 | Assent to a specific offer | `_sell_signal.wide_ask`: `is_affirmation(text)` AND `is_offer_shaped(our last outbound)` -> `WIDE_AFFIRMATION`, which arms `is_ask` and lifts `low_information` | `eligible` builds a `Turn` without `wide_ask`; assent is never read, so a bare "yes" sells nothing | basis `accepted_offer` only when our latest outbound is a receipted offer to this fan for an identifiable catalogue item that is still open, enabled, unowned, unexpired and unchanged, the fan's reply is a bare affirmation, and no refusal or other turn intervenes. The tease-dice branch is not enabled (no identifiable item) |
| 3 | Unrelated assent | "yes" only sells if our last message was offer-shaped (or a tease on a dice); "do you like dogs?" -> "yes" sells nothing | never sells | no sale; normal reply. Also no sale when the prior offer is a different/owned/expired/changed item |
| 4 | Ordinary chat | `kind == ""` or the hot / dare / rate arms; no price | `kind` always `""` (all signals false except `bot_accused`) | same text-only reply; `hot` beat available when `_common.thread_heat` is true; picture beats stay off (no vision in native: image analysis is unknown, never invented) |
| 5 | Buying signal (price ask, escalation, hot thread) | `is_price_ask` -> `WIDE_PRICE`; `escalation = sell.close and pivot_on_escalation and fan_msg_n >= min_fan_msgs_before_escalation_pitch and is_escalation`; `force_ask` (needs `gate_on`) on a hot thread | hardcoded false | price ask names a listed item -> `direct_request`; escalation / hot with the saved policy flags on and `qualify_snapshot` passing -> `policy_offer` (no fabricated quote); otherwise a reply only. No cold outreach, no paused fans |
| 6 | Refusal | decline classifier, `detect_spend_regret`, companion intent, `_objection.decide`, customs-owed (ai_chatter ~7990-8200); `fan_brake` is the pure form | `eligible` already calls `fan_brake`; nothing else | unchanged brakes, plus: a refusal after our offer cancels `accepted_offer`; a refusal bars `policy_offer`; kind never selects a pitch beat (`seller_off`) |
| 7 | Already owned content | `_open_offers_in_session`, `ownership`; unlocked-since-offer logic | `choice` -> `assert_unowned` drops the candidate; re-checked in `TextAuthority.prepare` and `_media_current` | unchanged, applied to every basis kind at queue time and at final dispatch; `ownership` fact recorded in the basis (`clear` / `unresolved_prebinding`) |
| 8 | Identity question | `detect_bot_accusation` -> `_TURN_BRUSH_OFF`, `seller_off` | `need_block` answers honestly as an AI clone; no sale | unchanged wording; additionally a sale on an identity turn is refused by deterministic validation (not only by the prompt) |
| 9 | New inbound during generation | not fenced (per-fan loop) | `NativeTextPlanner.prepare` re-checks `_valid_work`, versions, eligibility after the model; `TextAuthority.prepare` re-checks again | unchanged fences. The basis carries input/control revisions and policy revision; a stale plan cannot dispatch |

## Open evidence (commit 74ed1ee, pre-binding unlinked purchases)

74ed1ee stops `commerce.held` from holding on ledger `ppv_message` purchases with
no message id when they occurred before the current binding was created. That
excludes the 43 historical purchases from the hold. It does not resolve them.

- Which media those fans bought is unknown. `ownership.owned_media_in_session`
  cannot see purchases that have no message id, so ownership for those fans is
  unknown, not "none".
- Treatment chosen here: a per-fan fact. A fan with an unresolved pre-binding
  purchase gets `ownership: "unresolved_prebinding"` in the sales basis. Proactive
  `policy_offer` is refused for that fan; a fan's own `direct_request` or
  `accepted_offer` is still allowed, because he is asking to buy.
- A purchase with a NULL `occurred_at` is not treated as pre-binding (it holds).
- The cutoff is the CURRENT binding's `created_at`. A later rebind moves the
  cutoff forward; purchases between the old and new binding stop holding. This is
  residual risk and is pinned by a test, not fixed.
