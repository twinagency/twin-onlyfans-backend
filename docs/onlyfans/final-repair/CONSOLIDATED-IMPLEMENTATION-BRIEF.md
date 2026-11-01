# Original OnlyFans implementation: consolidated review scope

Source baseline: `claude/of-final-integration-20261101`, commit `a6b93bd227fec82589f955593811543889194613`.
Evaluation harness source: `claude/of-final-eval-20261101`, commit `64b6e802dae357609868cf80cbda1d64ebf7a83d`.
This continuation preserves Claude's integrated work. The separate Moxy workflow branch is excluded.

This document consolidates the supplied R0–R9 plan, Claude's attached handoff, and the two R5 clarifications supplied with this continuation. It does not claim access to the separate private execution-plan file mentioned in the handoff, or to uncommitted files on Claude's original workstation. Those sources must be reconciled before a release can be declared complete.

## Scope and authority

Implementation and isolated testing are authorised. Merging, deployment, production configuration changes, creator activation, real purchases and customer messages are not authorised. These remain separate R9 gates. No timer, silence condition, campaign or reference counter gains permission to send a new paid card.

Extend the existing contextual evaluator, normal conversation planner, SellLane, resolver, canonical quote and delivery authorities. No second seller, scheduler, pricing policy or queue. Keep Telegram and FLIRT behaviour unchanged by these continuation changes. Preserve valid prior hold, input-freshness, ownership, purchase-reconciliation, fairness, attribution, capacity and complete-answer repairs.

## R0: verified integration baseline

Verify current refs and exact file hashes, preserve the existing integration ancestry and distinguish source inspection from live state. A later operator must capture API/worker/supervisor revisions, image digests, schema, configuration revisions, account permissions, provider state, inventory readiness, ledger coverage and unresolved work using read-only transactions. A source branch is not evidence that its code is deployed.

## R1: shared action-aware holds

All internal senders use the existing shared policy and audited conversation controls. General stops, serious distress, consent concerns, identity review and human takeover block their appropriate actions. Hardship and material complaints suppress commercial pressure while policy-permitted support/chat can continue. Uncertain delivery remains under reconciliation and duplicate protection. Cases need evidence, an owner, due time, status and resolution; an unstaffed case is not resolved. External human/campaign activity invalidates obsolete AI work.

## R2: complete answers and bounded repair

Answer the fan's full current message, including multiple unanswered questions. Selling may inform the answer but may not replace it with an unrelated pitch. No compulsory tiny acknowledgement, interview, nickname sequence or question mark. Split at complete sentence boundaries and recheck freshness before each bubble. Preserve placeholder, identity, price and unsupported-media-claim guards. Repair is bounded; persistent invalid output enters visible review. Statistical reply-quality approval requires repeated real-model generations and independent review, not canned transports.

## R3: facts, memory, attachments and operator cases

Approved creator facts outrank history and model inference. Unknown facts remain unknown. Corrections retain provenance; creator/fan identities and photo subjects stay separate. Preserve received/processing/described/unsupported/failed attachment states; do not request an attachment already received or rate an unrequested image. Delivery disputes, identity questions and unsupported custom requests use R1's durable case model. Custom fulfilment requires verified availability, agreed scope/price, an approved payment route, an owner and deadline; missing requirements cannot become an automatic promise or charge.

## R4: real inventory and read-lane progress

Use the existing provider-read owner and bounded fairness, not extra provider workers. Readiness requires meaningful descriptions and valid media/version evidence. Product identity, exclusions, quantity, format, price source and available previews must remain truthful. A substitute requires agreement. Inventory imports and live readiness checks remain operational gates; code tests do not establish the state of a creator's live shelf.

## R5: active, bounded conversational progress

### Three approaches

On an eligible fan turn, choose a relevant verified paid offer, a specific real-item invitation, or naturally develop a relevant topic/preference. Ordinary conversation and necessary support remain valid. Do not rotate approaches mechanically or require an invitation before every offer. A creator's suggestion alone is not fan interest. Supported direct requests bypass pacing waits.

### Evidence versus proposed configuration

The historical first-offer observation is a pooled median of **3 preceding raw creator bubbles**, with middle half **2–6**. The paid card is excluded. These observations include sessions reaching an offer and multiple sender types; they do not prove optimal conversion. Creator medians in the supplied report are Alva 2; Katie, Amelia, Maile and Emma 3; Nicky 4; Cristina 5. Naomi has insufficient evidence for a creator-specific estimate.

For initial offline evaluation the proposed first window is **2–6 confirmed conversational bubbles**. The independent repeat reference is **3–6 completed creator replies**, labelled experimental. Neither reference is a mandatory wait. There is no “six replies plus two” rule and no conversion between counting units.

Per-creator controls are validated and persisted through the existing configuration API:

| Field | Stored key | Default / prepared value |
|---|---|---|
| `fasttCommercialInitiative` | `fastt_commercial_initiative` | standard; prepared pilot active |
| `fasttFirstOfferWindow` | `fastt_first_offer_window` | [2, 6] bubbles |
| `fasttRepeatOfferWindow` | `fastt_repeat_offer_window` | [3, 6] grouped replies |
| Existing contextual mode | `fastt_contextual_sales_mode` | off unless already explicitly saved |

Windows require two integers with 1 <= lower <= upper <= 50. Legacy scalar reference fields remain readable for compatibility, but do not derive or replace the independent windows. The active pilot profile is prepared data; no account is activated by importing it.

### Durable lifecycle and counting

Attempt state belongs to the existing `of_conversation_state` row. Start the first attempt on eligible fan participation under AI ownership. Record both bubbles and grouped replies. Count confirmed conversational output, including invitations, clarifications and filler. Filler still fails quality checks and cannot freeze allowance. Drafts, retries, duplicate events, receipts, purchase notifications and campaigns are not new creator replies.

A confirmed paid-card delivery closes the attempt, whether paid yet or not. A repeat attempt starts only on subsequent eligible fan participation. Payment does not start another turn or reset the attempt again. Questions, invitations, ordinary topic changes, nicknames, restarts, configuration changes and inactivity cannot reset an active attempt. Observe human activity and retain the shared history.

No-offer closure records its disposition, evidence and resumption condition. Reopening requires fresh verified relevant fan evidence or an actual blocker change, then current-context evaluation. Another generic input or a timer is insufficient.

### Upper boundary and one extension

Evaluate projected progress before releasing the complete response; a multi-bubble response may cross the boundary without being truncated. At or beyond the upper boundary, record one of:

1. A verified offer supported by current fan interest and eligibility.
2. A specific invitation for verified available content, retained as awaiting an answer without resetting counters.
3. At most one additional non-resolving clarification for a material product choice, a necessary product/price question, or genuinely ambiguous invitation acceptance. Record the exact missing information, item and supporting fan message. Known answers, ignored questions, rapport, teasing and rewording do not qualify.
4. An evidence-backed no-opportunity disposition or existing hold, with its resumption condition.

After the next relevant answer following the extension, resolve to an offer, unavailability, refusal/lack of interest, or the existing hold/support process. No second extension or generic tease. Invalid planning uses the bounded repair/review path and remains visible. Silence sends nothing.

### Invitations, acceptance and repetition

Store account, fan, source turn, actual item/assets, quoted amount, inventory revision and the confirmed invitation delivery. A bare yes, “send it” or suitable emoji accepts only the unambiguous current invitation. Revalidate availability, quote, ownership, current input and delivery eligibility; do not requalify that acceptance. Present a locked offer, never automatically charge.

Remember item refusals, general refusal scope, ignored invitations, offered media and confirmed purchases. Do not chase ignored invitations. A different item needs a genuine new opportunity; an earlier unpaid/rejected card is not a fan-wide ban. An uncertain delivery is not ordinary non-purchase. Budget scope and approved prices remain binding; an affordable suitable alternative may be considered without repeated bargaining. After purchase use the fan's response and actual remaining inventory, not an introductory script reset.

### Required visibility and verification

Record action, settings, evidence, item/invitation and exact deferral/failure reason. Distinguish planned offers, invitations, accepted delivery, confirmed purchases, pending answers, extensions, holds, no-offer attempts and failed delivery. Report first and repeat pacing separately, including attempts never reaching an offer.

Exercise real orchestration/persistence/authorities with isolated model/provider transports. Cover immediate request, proactive relevance, invitation plus short acceptance, gradual development, opportunities before/after the reference, no suitable opportunity beyond it, purchase plus a different item, refusals/ignored pitches/support/takeover, split replies/filler/restarts/competing senders, and mode/configuration changes. Prove both protection and eligible progress. Real-model transcript review remains distinct from synthetic dispatch tests.

## R6: one quote and fulfilment truth

Use integer cents, immutable accepted terms, existing discount policy and scoped fan budgets. The canonical quote authority finalizes native catalogue, pack and discount amounts while retaining the existing policy primitives. Freeze assets, versions, currency, price and obligation in the existing intent/receipt records. Track dispatch intent, provider acceptance, reconciliation, purchase and fulfilment separately. A timeout retains uncertainty; no blind resend or extra payment request. Delivery claims require evidence for the specific obligation. Refund decisions remain human-owned.

## R7: promotional traffic

Classify promotional/business intent from current evidence, not creator identity alone, a lone link, a discount question or repetition alone. Preserve raw history; exclude confirmed promo traffic from customer reply/memory/commercial measurement. Keep genuine, ambiguous, known-test and suspected-test cohorts distinct.

## R8: timing, fairness and capacity

Measure from earliest unanswered qualifying fan input to substantive response. Filler does not reset latency. Preserve oldest-need-first scheduling, cancellation recovery and existing bounded concurrency/leases. Test bursts, multiple creators, multi-bubble/media responses, restarts, 429s and uncertainty. The target of at least 95% of eligible turns within 180 seconds must be tied to a declared measured load. SQLite/synthetic results are not a production capacity guarantee.

## R9: release and supervised pilot gates

Offline regressions, adversarial transitions and full PostgreSQL checks precede release. New decisions begin in shadow only after separate operational approval. Retain the original two approved real-account delivery checks: exact assets, price, provider receipt and successful unlock. Then an explicitly approved, staffed single-creator pilot and creator-by-creator expansion.

Before approval, supply the candidate commit, configuration diff, schema plan, migration/lock assessment, read-only live parity snapshot, staffing, metrics and abort procedures. Abort on wrong recipient, unauthorised sending, duplicate send/charge, safety/identity failure, inconsistent price, lost fulfilment evidence or unenforced holds. Forward recovery must retain financial/receipt/ownership records; do not run an older incompatible image on the advanced schema.

Commercial evaluation compares net collected revenue per eligible fan with an explicitly recorded baseline, plus request-to-offer completion, orders, value, repeats, delivery success, complaints, reply latency and cost. Invitations and model proposals are not paid deliveries or revenue.

No merge, deployment, production settings change, activation, live purchase or customer message is authorised by this consolidated brief.
