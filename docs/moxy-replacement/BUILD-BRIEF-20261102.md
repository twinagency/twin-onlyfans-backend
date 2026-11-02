# Moxy replacement engine: build brief for Sol 6.1

Prepared 2 November 2026, Australia/Brisbane. This is the consolidated specification for the new Moxy engine. It supersedes earlier Moxy plans, handoffs, pacing amendments and contrary interpretations in historical reports. The original R0–R9 repair remains a separate infrastructure workstream.

## Goal

Automate Moxy's instructed chatter workflow, including active content selling, natural conversation, creator voice, prescribed sequences, accurate captions, delivery, purchase-aware continuation and customer care. Use the supplied common PDFs as the foundation and explicit creator-sheet instructions as subject-specific overrides, preserving their conditions. Where the creator sheet is silent, use the foundation. Historical messages illustrate execution; they do not override instructions.

Start with Amelia Bogovich and Cristina Rubino. Build one reusable engine: future creators supply their own sheet, account/catalogue mappings and operational information, without a copied engine or creator-specific code branches. Do not invent a new sales strategy or promise identical earnings.

## 1. Establish one compatible implementation base

Repositories: `twinagency/twin-onlyfans-backend`; `twinagency/twin-dashboard` only for necessary existing-UI changes. Read `SOURCE_CHECK.md` in the package, then refresh the listed heads and relevant live configuration. Work in a dedicated integration branch/worktree; preserve other ongoing work and data. The repository default is not the latest repair baseline.

Reconcile current concurrency, history/memory, onboarding and schema work before integrating. PR #78 already contains a migration merge, but predates the latest checked concurrency commit. Reuse valid work; do not recreate or overwrite it. If production access is unavailable, continue isolated implementation and explicitly block release-dependent verification.

## 2. Turn the sources into reusable configuration and examples

Read all five packaged PDFs and the completed conversation findings. Create versioned common instructions plus creator configuration covering voice, facts/privacy, permissions, products/prices/sequences, fan exceptions and operating conditions. Link each mapped instruction to its source page. Keep unresolved fields explicit; historical claims and blank placeholders cannot fill them automatically. Explicit creator overrides must work through validation and execution, not just appear in a prompt. Same-creator ambiguities still require resolution. Identity questions retain the agreed pause-and-owned-handover behaviour, not a false automated denial.

Use the full saved evidence to prepare separate **compliant examples**, **prohibited examples** and **uncertain cases**. Classify exchanges with surrounding context, not entire threads as uniformly good/bad. Record source message IDs, applicable rule, rationale and expected behaviour. Examples teach decisions and style, not mandatory wording or a vocabulary whitelist. Negative examples belong in labelled evaluation material, not approved writing references. Reserve separate conversations for testing generalisation.

Preserve source records and legitimate repeated scripts. Do not apply the previous ten-fan repetition exclusion, six-hour session split, keyword gate or another unexplained filter as behavioural policy. Any analysis grouping must retain originals, disclose its purpose and not silently remove relevant execution patterns.

## 3. Replace the legacy decision paths

Build one Moxy policy owner and authoritative writer for replies and contextual captions. Feed it applicable instructions, recent conversation, verified fan memory, current offers/ownership, sequence position, obligations and real inventory. It chooses the appropriate permitted action and produces matching text.

For migrated accounts, route all automated conversation and commercial entry points through this owner: replies, welcomes, invitations, paid offers, scheduled engagement, upsells, follow-ups, purchase continuations and campaign decisions. Preserve manual takeover. Existing external tools cannot be assumed controllable; document and verify their coordination before overlapping automation is enabled.

Reuse proven CRM storage, connections, media handling, accounting, receipts, transport, concurrency and operator controls. Replace or detach legacy prompts, interviews, brevity/humanizer rewrites, keyword sales gates, discount ladders, offer windows and competing caption writers from the Moxy path. Do not bypass genuine ownership, privacy, payment, safety or transport protections. Align commercial validators with Moxy's configured terms rather than leaving obsolete policy underneath the new prompt.

Exactly one engine may own a migrated account. No silent fallback to the old writer. Cancel or revalidate queued legacy commercial jobs at cutover; preserve accepted deliveries, purchases and fulfilment. Remove demonstrably unused legacy components in scope after checking callers. Shared or unmigrated-account dependencies remain isolated until migrated; do not delete unrelated CRM functionality, history or rollback evidence.

## 4. Reproduce the selling workflow and media handling

Actively develop appropriate opportunities using Moxy's method: fulfil direct requests promptly, invite interest in a specific available item, or naturally lead into a relevant offer. Interpret acceptance against the retained invitation. Acceptance may produce a paid card, never an automatic charge.

Follow actual prescribed sequences and verified purchase events, including returning customers and permitted continuation without another incoming message. Distinguish individual offers, promotions, requested resends, alternatives, corrections and paid-service fulfilment. Respect the applicable no-push, ordinary-chat, refusal, budget, hardship and escalation instructions. Investigate ordinary complaints and use authorised remedies; do not impose a blanket hold where the sources permit a legitimate resolution.

No invented offer-every-N-messages rule, forced warm-up, post-purchase waiting period or generic discount schedule. Retain documented creator-specific timing and approved operational reply settings. Track unanswered buying questions, accepted invitations and outstanding obligations until resolved. Surface stalled work with its actual cause. Compare comparable historical behaviours, not a filtered aggregate median; counters must not create interest or force a send.

Bind actual assets to products, sequence steps, access requirements and approved prices. Generate evidence-based internal media descriptions on import/change and reuse them. Keep these separate from fan-facing captions. The writer adapts captions using verified media facts, applicable scripts and the current conversation. Do not invent unseen video actions, custom/live status, scarcity or bonuses. The card already displays price and attachment count; repeat them in text only when useful or asked.

Use exact available purchase/ownership evidence. The supplied export lacks reliable PPV-to-card links; similar price/time is not proof. Maintain durable account/fan state and reconcile uncertain outcomes. Unknown information blocks the affected action with a visible reason, not every unrelated conversation. First locate missing records in existing systems; escalate only genuinely missing authority or essential data.

## 5. Prove replacement, correctness and scalability

Test both creators through the real integrated path, including natural paraphrases and unfamiliar situations. Cover direct requests, invitations/acceptance, proactive progression, purchase continuation, returning fans, refusals, resends, ownership, complaints, fulfilment, privacy and handover. Test creator overrides and foundation fallback independently. Demonstrate adding a third synthetic creator by configuration alone, without live activation.

Verify actual asset/price/caption agreement and delivery/purchase state separately. Test new input during generation, human takeover, campaign interleaving, two workers, duplicate events, provider timeouts after acceptance, restart, stale products/prices, missing attachments and reversals. Show that legacy writers and sales timers cannot run for Moxy accounts, including queued jobs and failure paths.

Use isolated PostgreSQL and provider fixtures, plus bounded real-model evaluations within an explicit recorded test budget. No unbounded paid processing or resumption of paused imports. Preserve independent fan preparation/delivery, per-fan ordering, tenant isolation and provider limits. Measure multi-creator load and latency against the recorded supported load and approved settings. Unit tests alone do not establish model quality or live delivery.

## 6. Return one reviewable release package

Complete implementation and testing independently through routine decisions. Return the branch/commits, source mappings, example/test records, measured results, remaining blockers and a short list of legacy behaviour removed or isolated. Extend existing controls only as needed for engine selection, Off/Preview/On, readiness, decisions and owned handovers. Preview must not send or advance live purchases.

Provide the configuration diff, cutover procedure and schema-compatible disable/rollback plan. Keep purchases and obligations intact; disabling Moxy must not silently enable a legacy engine. Initial rollout scope is Amelia/Cristina only. Additional creators use the same configuration onboarding and readiness checks.

Do not merge, deploy, activate accounts, change production settings, make purchases or send customer messages without separate authorisation. Report built, tested, deployed, enabled, delivered and purchased as distinct states. Do not claim completion while required functions are stubs, missing mappings or unexecuted tests.

**Done means:** the integrated engine performs Moxy's permitted conversation and commercial workflow using real account state, has no competing legacy decision path, and has evidence for the claimed functionality and limits. Passing exact scripted responses alone is insufficient.
