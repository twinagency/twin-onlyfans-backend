# R1: external senders and the one-active-sender requirement

Status: written with the R1 implementation. Applies to Infloww, the OnlyFans app or website, and any other tool that
can post to the same creator conversations as this system.

## What is enforceable and what is not

Inside this system there is one conversation authority. Every internal sender (reactive chat, pack and upsell cards,
contextual offers, follow-ups, operator campaigns, proactive engagement, manual operator messages) passes the same
policy (`onlyfans_runtime/conversation_policy.py`) at plan time and again immediately before the provider call.
Two workers cannot both own one turn: the conversation work lease, the delivery policy row lock and the single
active-intent admission decide one winner, and the loser is cancelled by the control or revision check.

An external tool is outside that authority. It does not read our conversation state, our takeover state, our leases or
our delivery ledger, and we cannot make it. List membership (the PR #70 ownership-list mirror) is visibility only; it does
not stop Infloww from sending, and nothing here claims an external lock.

## What R1 does about it (observe, record, cancel)

* Observation: before any non-manual send (`prepare`, `admit`, `consume_dispatch`, the FASTT turn) the shared scan
  (`operator_cases.scan`) looks at the fan's recent outbound messages. An outbound message that no internal sender
  wrote (no `of_message_delivery` row, no accepted `of_text_intent`, no sender label, no mass-run id, and older than a
  60 second grace so that our own send's projection cannot be mistaken for it) is an external message.
* Recording: the observation is written to the same durable conversation state both planes read. With the creator's
  `conversationSafetyMode` at `shadow` (the default) it is recorded as a `shadow` row in `of_operator_case` and nothing
  else changes. At `live` it opens an `external_sender` case, pauses automation through the existing audited
  `Store.control`, and raises `requires_attention`.
* Cancelling obsolete internal work: the pause bumps the control revision, which makes every plan, queued intent and
  campaign or engagement item that was prepared against the old state obsolete at its next `admit` or
  `consume_dispatch` (reason `snapshot_changed`), and cancels pending engagement actions. A person hands the chat back
  with the normal control; that hand-back closes the `external_sender` case.
* Both planes agree: the worker plane reads the case through `TextAuthority._eligibility`; the FASTT runner plane reads
  it through `fan_hold_reason`. They are two callers of the one policy.

## The unavoidable race

Between the moment an external tool posts and the moment we observe it there is a window. We learn about an external
message only when the provider history reaches us (sync or the runner's history preflight), so:

1. An internal message can be in flight at the same instant an external one is typed. Both reach the fan. We cannot
   prevent that; we can only record it afterwards, and the idempotent delivery ledger ensures the internal one is
   not sent twice.
2. The 60 second grace means an external message younger than that is not yet judged.
3. A fan message that asks to stop, and that arrives while an internal turn is already past its last pre-wire check, can
   still be answered by that one in-flight turn. The pre-wire check closes the window to the length of one provider call.

## Operating requirement: one active sender per creator account

Until a shared authority exists, autonomous coordination must not be claimed unless this arrangement is in place:

1. For each creator account that runs autonomous replies, exactly one system is the active sender at any time. The
   arrangement is written down per account (who owns sending: this system, or a named human or tool).
2. When a person or an external tool needs to write, the operator first takes over the conversation in this system
   (`request_takeover`), or pauses the account, so internal senders stop before the external write.
3. Infloww auto-reply, mass messages, scheduled messages and drip campaigns for the same creator are disabled while this
   system is the active sender, and this system's campaigns are disabled while Infloww is.
4. `conversationSafetyMode` is set to `live` for the account so that an external message that does happen is observed,
   pauses automation and becomes an owned case. At `shadow` the observation is only a record.
5. Cases are watched: an `external_sender` or any other case has an owner and a due time; overdue cases escalate
   (`escalationLevel`) and are visible in `GET .../operator-cases`.

An account that cannot satisfy items 1 to 3 must not be described as autonomously coordinated.
