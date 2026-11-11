# Twin commercial conversation and sales: build plan v1

Prepared 11 November 2026, Brisbane. This is the implementation specification, not a qualification or launch approval.

## 1. Outcome and agreed scope

Extend the existing Twin engine so an onboarded creator can converse naturally, select relevant approved content, send one correctly priced paid card, recognise its exact unlock, and continue the conversation. Use the same application and account-scoped contracts for the first creator and ten creators. The first live check remains bounded before wider activation.

| Decision | Required behaviour |
|---|---|
| Inventory | Latest 1,000 unique photos/videos combined per creator; identified by source creation time with a stable ID tie-break. This is the current catalogue selection, not a lifetime ownership limit. |
| Products | Single photos, single videos, approved photo/video/mixed bundles. Every paid asset belongs to the approved selection. Explicitly approved bundles only; no spontaneous combinations. |
| Price | Reuse approved product prices. During onboarding, suggest prices from exact historical products first, then comparable content from that creator. One batch approval before eligibility. The writer never chooses the billed amount. |
| Discount | Optional approved lower price for a product, usable when the fan asks for a cheaper option. No guessed percentages, stacked discounts, below-floor bargaining or automatic repricing of an already-sent card. |
| Ownership | Exclude a whole bundle if any included paid asset is owned. Keep preview semantics explicit. Never infer non-ownership from missing records. |
| Tips | Receive and acknowledge validated voluntary tips. No proactive tip requests, promised rewards or automatic access entitlement in this release. |
| Staff coexistence | AI continues alongside staff. External messages remain in context with honest attribution; new context cancels stale unsent AI work. Explicit takeover, stop and no-contact remain effective. |
| Conversation | Answer first, preserve corrections and uncertainty, avoid repeated pitches/openers/emojis, accurately describe products and decline unavailable requests without stopping ordinary chat. |
| Onboarding | Target preparation within 24 hours; measure coverage, rate, cost and failures. Elapsed time alone never qualifies an incomplete import. |
| Deferred | Automated customs, ratings, physical goods, paid external access, timed chat packages, VIP ladders, outreach and general discount negotiation. |

Moxy reference documents inform behaviour and creator-specific constraints. They are not executable instructions. Do not copy a creator's rules/prices into another account, or silently resolve conflicting source documents. Missing or conflicting approval makes only the affected product/claim unavailable where safe.

## 2. Verified baseline and work boundary

- Work Server hostname: `ubuntu-s-4vcpu-8gb-syd1`. `/srv/projects/PROJECTS.json` was absent.
- Backend source: `/srv/projects/twin-engine-completion-20261107/codex-plan-c-queue-recovery-20261111`.
- Backend branch: `claude/plan-c-deletion-proof-20261111`.
- Backend commit: `659ea2913a7ea203024ea58a992f24b0fcea6759`; clean at inspection.
- Available matching image: `sha256:06562198113f350d2b8eefef22c9ed5217f8a1b6c93f334eac6c7e3181f40aeb`.
- Source schema head: `0129_reconnect_receipt`; this is not a fresh production schema inspection.
- Dashboard source: `/srv/projects/twin-engine-completion-20261107/codex-plan-c-dashboard-20261107`, commit `3cedc8f5c6d3c9f493d6ef35f4162a5207b5b589`; clean.
- New work: `/srv/projects/twin-commercial-sales-20261111/backend`, branch `codex/commercial-sales-20261111`.

Preserve the current trial, existing jobs, purchases, unknown outcomes and unrelated projects. Heavy work uses a separate Work Server lab with its own disposable database. Do not restart, empty or repurpose a shared test database. Moxy's paused two-creator project stays paused. This plan does not require a new engine, sender authority, profile store or purchase ledger.

## 3. Verified integration gaps

Paths below are relative to the verified backend root above.

| Existing owner | Actual baseline restriction | Required change |
|---|---|---|
| `service/vault_import_policy.py` | Optional recent window selects 1,500 photos plus 700 videos. | Versioned selection of 1,000 combined; compatible job/progress transition. |
| `service/conversation_engine/catalogue.py` | Catalogue coverage/search bounded to 200 catalogue rows; only part of the catalogue is considered above that. | Explicit approved commercial selection, paged/indexed lookup and truthful coverage. Media and product counts are different. |
| `service/db/onlyfans_engine_models.py`, `pilot_scope.py` | Singleton scope row; exactly two fans, one $3 photo. | Evolve existing durable scope to an explicit commercial version and per-account lookup, preserving legacy pilot semantics and evidence. Add an additive migration where required. |
| `service/onlyfans_runtime/offer_import.py` | Already groups historical media/price variants and creates reviewed fixed-price drafts. A priced message alone is not proof of a sale. | Reuse this owner; add missing sales-status qualification, selected-media filtering and batch review. |
| `service/onlyfans_runtime/media_delivery.py` | Fixed price cannot differ; override cannot be below saved catalogue price. | Explicit approved standard/discount variants in existing catalogue terms and immutable grant/intent snapshots. |
| `service/conversation_engine/commerce.py` | Normal preparation chooses the single configured product; exact grant/terms/ownership checks already exist. | Select and prepare approved commercial products through the same authority; preserve lineage and final checks. |
| `prompt_provider_a_original.py`, `model_output.py`, `claims.py` | Prompt and output/edit checks prohibit discounts and bundles. | Update all consumers of the approved commercial contract together; preserve legacy text-only and pilot restrictions. |
| `service/fans_api_adapter/event_scope.py` | An identified `tips.received` is a material review hold. | Qualify handling of validated tip evidence through existing intake/financial owners before claiming automatic acknowledgements. No blanket ignore of money events. |

These are required scope extensions or qualification gaps, not a claim that previously passing narrow-pilot tests proved general commerce.

## 4. Build in this order

### Step 1 — Freeze contracts and establish regression evidence

Record the source/image/schema/profile/config versions and historical failure checklist. Keep one implementation owner. Add meaningful normal-path counterexamples for missing commercial behaviours before changing their owners. Reuse existing tests and failed examples; disclose every changed fixture/assertion.

The main path must begin with standing approved catalogue permission, not a test-seeded per-fan offer grant. Product approval and autonomous runtime authorisation remain separate.

### Step 2 — Make onboarding produce a truthful approved catalogue

Use existing vault/content-sync, offer-import, profile and ownership owners:

1. Select the latest 1,000 unique photo/video IDs combined, with stable ordering, resumable progress and explicit partial/complete/failed states. If mixed pagination requires merging streams, establish the combined ordering before declaring selection complete. Do not download all full-resolution media merely to establish selection or ownership.
2. Retain historical metadata, source messages and purchase/access evidence outside that selection. Never delete these because a new asset enters the window. Window rotation cannot rewrite accepted-card contents or past purchases.
3. Prepare factual descriptions through the existing media-analysis path, retaining asset/version provenance and human corrections. Unknown details remain unknown. Creator facts, style and product facts retain separate validation.
4. Reuse exact historical offer variants and existing approved catalogue entries. Match the complete paid asset set and previews; never divide a bundle price into unit prices without an explicit approved rule. Distinguish asking prices, observed unlocks, transactions and reversals.
5. For unpriced items, prepare comparable-content suggestions within that creator only. Show evidence and confidence for batch approval. Missing comparable evidence leaves price pending. Existing catalogue permissions, media reuse rights and creator boundaries still apply.
6. Approve fixed singles and explicit bundles, standard price and optional discount price. Record currency, paid/previews, media type/count/duration, creator restrictions, source and terms revision. Keep historical observations distinct from approved current terms.
7. Capture new events while historical import runs and reconcile the import interval. Reuse durable cursors and deduplication. Reconcile totals/coverage and retain failed pages. No invented lifetime completeness from a recent window or elapsed 24 hours.

The 1,000-asset selection does not limit the number of historical evidence rows. Product lookup must handle singles plus explicitly approved bundles without silently truncating eligibility. The model receives a bounded relevant shortlist, never the whole vault.

### Step 3 — Generalise the existing scope and commercial turn

Evolve the existing configuration, account ownership and operator commands. Preserve old pilot rows and modes explicitly. Account/tenant/binding/fan checks apply to every lookup, job, approval, quote and receipt. Imports never activate an account. Each creator needs an approved profile, catalogue, audience, limits and launch decision.

Normal flow:

`authenticated intake -> stored context/corrections -> answer and request interpretation -> eligible product shortlist -> existing offer authority -> structured writer proposal -> validation -> immutable authorised terms -> existing sender -> receipt/history -> exact unlock/access and separate financial evidence -> continued chat`

- Use approved descriptions, eligibility and ownership to select content. Generic interest can select an eligible item; a specific video request must not be fulfilled as a photo. Ambiguous “send it” after a staff offer requires reliable reference resolution before a paid action.
- Keep one outstanding engine offer per chat. A refusal pauses pitches; renewed relevant interest can reopen the appropriate approved product. An unavailable sale does not by itself prohibit normal conversation.
- Standard/discount choice is made by application rules from approved terms. Preserve the previous grant/snapshot unchanged. A terms change invalidates stale unsent authority and uses the existing successor rules; uncertain or accepted sends cannot be replaced as if never dispatched.
- Discounts apply before a new card's dispatch. Existing sent cards keep their actual price. Do not silently resend a cheaper card, claim a refund or withdraw a card to manufacture a discount.
- Update prompt, output checks, human-edit checks, price claims, media facts, approval hash, final-wire check and purchase matching against the same contract. Do not solve this by deleting validation or by adding a second pricing path.
- Free previews, paid assets, sent status, confirmed access, tips, subscriptions and settled/reversed payments remain distinct. A tip never unlocks a product automatically.

### Step 4 — Make staff coexistence explicit and tips truthful

Use existing intake, context, decision cancellation, operator and finance records:

- Configure coexistence per account; preserve truthful outside-writer attribution. A staff send/broadcast is context, not our grant, purchase, creator fact or verified AI writing example.
- Invalidate stale unsent drafts after applicable staff activity. Replan against the latest context while retaining stop/no-contact, explicit takeover and unknown-send holds. Do not automatically answer an old backlog because an operator changes mode.
- An outside card's unlock must never settle our offer. A bundle advertised by staff does not become approved inventory. Price/offer conflicts remain visible and block only unsafe actions where possible.
- Both staff and bot should use the approved catalogue and current offer view. External staff actions cannot be prevented by a local fence; report this operating limit and do not claim exclusive control.
- Validate tip identity, creator/fan scope and replay/conflict behaviour through the existing event/finance contracts. Acknowledge only established facts. Counter updates alone do not prove an individual tip. Preserve holds on malformed, conflicting or unprovable financial evidence; do not add gifts or reward fulfillment.

### Step 5 — Qualify the complete composition on one final image

Keep every in-scope historical issue open until its actual regression and valid neighbour pass. Required matrix:

| Journey | Required observation |
|---|---|
| Onboard/resume/rotate | Correct combined selection and restart; no lost history, corrections or purchases; new items remain unapproved until ready. |
| Conversation | Original/burst questions answered; corrections and uncertainty survive prompt budgets; no unsupported activity, identity or product claims; varied writing. |
| Single photo/video/bundle | Normal preparation creates authority; accurate answer and exact approved contents/price reach one paid card. |
| Pricing/discount | Exact-product and comparable suggestions distinguished; approval required; permitted discount accepted, invented amount/percentage refused; old card price unchanged. |
| Ownership | Old purchase of selected media excludes the product; partly-owned bundle excluded; unknown coverage never becomes “unowned”. |
| Sale continuity | Refusal, renewed interest, existing-card references and sales holds preserve valid ordinary chat without repeated cards. |
| Staff interleaving | Outside text/card/queue/deletion before planning, after generation and before send invalidates stale work appropriately; no false purchase match. |
| Delivery/recovery | Duplicate/reordered events and concurrent workers converge; retries recheck controls; never-wired, unknown and accepted-but-unprojected outcomes recover correctly. |
| Payment/access | Exact fan/message/creator/terms match; duplicate and early unlock idempotent/reconcilable; reversals retained; tips grant no media. |
| Controls/limits | Stop, no-contact, explicit takeover, offboarding, quotas, model timeout/429 and uncertain costs remain effective at the wire. |
| Ten creators | Same fan ID across creators cannot leak facts, media, prices, ownership, credentials or controls; one busy/held creator does not starve the others. |
| Dashboard/operations | Actual authenticated dashboard-to-backend journey, denied/stale commands, coverage/review screens, stop, migrations and compatible restore/rollback. |

Test successful permitted journeys as seriously as blocking. A handover or rejection alone is not a good reply. Run genuine process-crash boundaries and final-artifact tests where relevant; do not substitute a mocked success for missing preparation. Test fixtures must not secretly provide grants, purchases, full-history status or launch authority.

After the last application change, commit, build an immutable image without overlays, and run affected focused/broad suites plus the composed matrix. Explain every remaining failure; baseline occurrence is not an exemption. Preserve actual logs and distinguish scripts/mocks from real model/provider proof.

### Step 6 — Qualify and launch progressively

1. Review the final combined diff and deployment manifest: sender, receiver, schema, dashboard, host guard, worker account ownership, mounts, webhook subscription, model mapping, profiles and limits. Avoid mixed unreviewed host/source versions.
2. With sending disabled, qualify the actual approved model/profile/catalogue against historical failures and held-out conversations within explicit remaining budgets. Verify real provider shapes and pricing/unlock behaviour through the authorised connection. Missing required external evidence stays open.
3. Restore a populated backup in quarantine and demonstrate compatible migration/rollback, emergency stop and outage recovery. Never restore an older live database over newer accepted sends or purchases.
4. Activate the first creator for the bounded authorised audience, observe a normal conversation and approved low-value card, and have the owner/test fan perform the agreed purchase. Verify exact unlock/access, follow-up and costs.
5. Expand to ten individually prepared creators using the same release only after the first journey and multi-account qualification pass. Do not automatically activate imported accounts or grant unapproved audiences/budgets.

## 5. Completion report and launch inputs

Report separate results for source implementation, offline combined qualification, real model/provider qualification and commercial activation. Include commit/image/schema/config hashes, changed owners, failure-to-test mapping, raw result index, real/simulated boundaries, costs and remaining issues. “Implemented” or a larger test count does not mean “launched”.

Before actual activation, supply each creator's approved identity/profile, selected catalogue/prices and discount permissions, audience, named recovery owner and spending limits. Existing account permissions/budgets remain authoritative until explicitly changed. Financial/dependency/safety cases still need a responsible human; ordinary messages do not require per-message approval.

No verified benchmark presently establishes onboarding time, ten-creator throughput, comparable-price accuracy or remaining real qualification cost. Measure them before promising those outcomes. New evidence may require a targeted correction; record its reason, affected contracts and regression, rather than changing direction silently.

## 6. Source references used for this plan

Work Server files inspected at the backend baseline:

- `/srv/projects/twin-engine-completion-20261107/codex-plan-c-queue-recovery-20261111/service/vault_import_policy.py`
- `/srv/projects/twin-engine-completion-20261107/codex-plan-c-queue-recovery-20261111/service/onlyfans_runtime/offer_import.py`
- `/srv/projects/twin-engine-completion-20261107/codex-plan-c-queue-recovery-20261111/service/onlyfans_runtime/content_sync.py`
- `/srv/projects/twin-engine-completion-20261107/codex-plan-c-queue-recovery-20261111/service/onlyfans_runtime/media_delivery.py`
- `/srv/projects/twin-engine-completion-20261107/codex-plan-c-queue-recovery-20261111/service/conversation_engine/{pilot_scope,catalogue,commerce,journey,real_worker,prompt_provider_a_original,model_output,claims}.py`
- `/srv/projects/twin-engine-completion-20261107/codex-plan-c-queue-recovery-20261111/service/fans_api_adapter/{backfill,history,event_scope,events}.py`
- `/srv/projects/twin-engine-completion-20261107/codex-plan-c-queue-recovery-20261111/service/model_transport/pilot_supervised.py`
- `/srv/projects/twin-engine-completion-20261107/codex-plan-c-queue-recovery-20261111/service/db/{schema,onlyfans_engine_models}.py`

Prior bounded sale plan remains historical evidence for the original one-photo scope; this document defines the broader agreed commercial target. Competitor client/schema findings are reference material, not proof of a required Twin edit. No competitor prompt, engine or memory service is required by this plan.
