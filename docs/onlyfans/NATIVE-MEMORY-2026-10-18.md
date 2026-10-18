# Native memory evidence — 18 October 2026

Current offline schema: `0076_native_memory_evidence`. This extends 0075's
retained model-budget proof. Only an explicit synthetic model is registered.
No paid model, real account/provider, remote database, deployment, cap reset or
Telegram runtime was activated or changed.

## One context and one generation

The existing native planner now snapshots the account/fan's existing
`FanProfile` alongside approved creator configuration, typed `Fan` facts and
the latest 100 non-retracted canonical messages. The same 64 KiB context and
20,000-byte model-output limits apply. Profile reads join the configuration/fan
fingerprint under shared row locks; a newer profile also invalidates a prepared
or admitted reply. Fan language uses the existing native language resolver,
including its per-fan override and localized identity detector.

Fastt's existing prompt composer owns the scoped reply-and-memory output
contract. One response contains `reply`, `facts` and `profile`; only the guarded
reply becomes a text intent. There is no second extraction, summary or rewrite
model call, extra conversation engine or old Twin prompt layer. Legacy callers
retain the default text contract. Deliberate typo injection stays disabled.

## Canonical correction and provenance

The current-value owners remain `Fan` and `FanProfile`. Seven bounded typed
fan fields can be proposed: real name, adult age, city, country, hobbies,
occupation and preferences stored in the existing `fetishes` field. No transport,
account, control, money, creator-persona or configuration field can be proposed.
Facts must be nonempty strings within the prompt's existing bounds. Age proposals
must be explicit adult ages, 18–120. Empty/sparse output does not erase data.
Deletion/forgetting is not inferred from a missing value.

Each proposed change supplies a nonempty exact quote from the claimed latest
inbound. The worker assigns account/fan/message/input/plan identity; the model
cannot choose those references. Quotes from older turns, another conversation,
changed or retracted sources are refused. New short biography/bullet notes use
the existing profile columns, bounded to 400/600 characters. Unrelated profile
questions, metered openers, labels and counters are preserved.

Schema 0076 adds only `of_native_memory_change`, an audit of before/after values,
source quote/message, canonical input revision, plan and original context hash.
Foreign keys retain the source message and plan. PostgreSQL rejects audit UPDATE
and DELETE through an append-only trigger. This is provenance, not another
writable memory store or a separate conversation history.

Only a still-current lease, binding, configuration/fact/profile snapshot and
settled matching model call can apply a proposal. Existing canonical values,
audit rows and ready-plan transition commit together. The plan retains its
original generation context JSON and advances its dispatch fingerprint after its
own changes, so its own learning does not invalidate the reply. A crash cannot
leave a successful partial correction without its provenance and plan state.
Stale, timed-out, uncertain-cost or malformed replies cannot update memory;
known generation cost is still retained when output or source checks refuse it.

Learning from a fan's inbound does not require sending a reply. It does not
claim that any outbound message, sale or purchase occurred. Only the existing
accepted-receipt path creates canonical outbound messages. The prompt gives
latest explicit corrections priority over typed facts, and typed facts over old
profile notes. Approved creator facts continue to outrank earlier invented
creator claims. Exact quotes establish provenance, not independent verification
of the model's semantic interpretation.

## Migration and recovery

Frozen 0070–0075 snapshots remain unchanged. Supported old revisions upgrade
without activating work, changing caps, settling unknown calls or rewriting old
plans. A populated 0075 rehearsal preserves all prior plan/cost/history/control
rows. Old prepared input is retained rather than regenerated; its older context
fingerprint cannot authorize a new wire.

An actual encrypted pg_dump/restore rehearsal includes corrected facts, profile,
cost and audit rows. They survive byte-equivalent relational comparison, the
append-only trigger survives, and the restored database remains quarantined.
There is no recovery-release command or automatic sender restart.

## Remaining gates

This is bounded synthetic text/memory, not complete Milestone 4. Full accepted
style/configuration policy, actual paid provider credentials/routing/usage,
semantic quality acceptance, explicit operator fact edits/forgetting and legacy
writer retirement/import remain open. Source quotes cannot certify natural
language extraction accuracy. Existing photo/video commerce and authenticated
commands, purchase/refund/ownership evidence, operational scale/soak and release
gates also remain open. No new live permission follows from offline tests.
