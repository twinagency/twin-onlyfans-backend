# Claude takeover: Twin conversation and commercial sales

Prepared 11 November 2026 for Jesse, sent to Ronan for Claude.

## 1. Ownership and authority

Jesse has assigned Claude all repairs and completion of this commercial build, replacing Sales slave/Sol. ChatGPT remains the independent reviewer. Sales slave was instructed to stop implementation, preserve work/evidence and leave a checkpoint. Verify that checkpoint before editing. Do not run two builders against the same owners or production instance.

This is an implementation handoff. Continue the authorized offline repairs/build and qualification. This email itself does not add production deployment, activation, customer-message or paid-call authority. Existing explicit approvals remain valid within their actual scope; verify them rather than asking for the same approval again. Missing credential references or per-creator spending approvals are real inputs, not values to invent.

**Current objective:** finish the existing conversation engine plus a reliable commercial sales path for ten creators, using the same account-scoped code as the first creator. The first launch must prove a bounded real sale before wider activation. The new preparation milestone is not a finished sales system.

## 2. Start here and exact source

Use `ssh work-server` from Jesse's machine. Server: `134.199.145.233`; the hostname verified by the reviewer is `ubuntu-s-4vcpu-8gb-syd1`. Existing authorized SSH access is needed; credentials are not attached or committed.

Project: `/srv/projects/twin-commercial-sales-20261111`.

| Component | Exact reviewed version |
|---|---|
| Backend directory | `backend/` |
| Backend branch | `codex/commercial-sales-20261111` |
| Backend HEAD | `7aa81dceff53b5ef4fc98feb085d25467e783d15` |
| Dashboard directory | `dashboard/` |
| Dashboard branch | `codex/commercial-sales-dashboard-20261111` |
| Dashboard HEAD | `b77ec6d1eb6c9ae4ed6c3f54753dc0d804e8a969` |
| Candidate image | `sha256:86108bebac6faa3634c012b6d493faee1eb231f207a59e5f95df10b2687d376a` |
| Candidate schema | `0132_account_preparation` |
| Application base | `659ea2913a7ea203024ea58a992f24b0fcea6759` |

Both working trees were clean at the reviewer's takeover snapshot. The milestone was reported as unpushed, undeployed and inactive; do not assume GitHub already contains these exact commercial commits. Read actual remotes and compare remote refs before any publication. Current source is on Work Server; do not start from an older emailed ZIP or blindly from main.

Reading order:

1. This file: current ownership, required outcomes and repair order.
2. `CLAUDE-TAKEOVER-CHECKPOINT-20261111.md` in the project root: builder's final stop state, if present. Reconcile any newer files/commits with the reviewed versions; preserve them.
3. `/srv/projects/twin-commercial-review-20261111/REVIEW.md`: independent findings, evidence and limits.
4. Project `PREPARATION-MILESTONE-REVIEW-20261111.md`, `PREPARATION-CANDIDATE-20261111.json`, and `evidence/PREPARATION-RESULT-INDEX-20261111.json`.
5. Backend `docs/plan-c/COMMERCIAL-SALES-BUILD-PLAN-v2-20261111.md`: detailed approved commercial specification. Its older baseline-gap table is historical; later preparation work resolves some of those gaps. This takeover file and the independent review describe current status.

The older project `BUILD-CHECKPOINT-20261111.md` describes the initial 3ce1a7e increment, not the final preparation candidate. The previous reviewer-to-Sales-slave continuation message is superseded only as to implementation ownership: Claude is now sole builder.

## 3. Product contract to preserve

- Extend the existing Python backend, SQLAlchemy/PostgreSQL/Alembic owners and TypeScript/Next.js/React dashboard/BFF. Reuse intake, context, vault jobs, catalogue, approvals/grants, delivery authority, receipts, purchase/access records, budgets and operator controls. Do not create a second conversation engine, sender, pricing authority or purchase ledger.
- Same tenant/account/binding/connection/fan scoping for one and ten creators. Identical fan/media IDs in separate accounts must not share context, permissions, prices, purchases or jobs. Bound concurrency and prove fairness; ten-account isolation alone is not a throughput benchmark.
- Import the latest **1,000 unique photos/videos combined per creator**, ordered by source creation time plus stable ID. Preserve historical metadata, conversations, corrections and purchase/access evidence outside that window. A recent catalogue window is not lifetime ownership coverage.
- AI proposes factual descriptions/categories. Uncertain, low-confidence, stale or unbound analysis goes to Miscellaneous/unresolved, with an honest reason and no automatic sale approval. Partial video sampling cannot prove every frame or action.
- **Agency manually chooses category single-asset prices.** No AI price generation or automatic historical-price inference. Singles: photos and videos. Bundles: explicit approved photo/video/mixed asset sets with separately approved total prices. No spontaneous bundles.
- Optional discounts use an explicitly approved lower price when the fan asks. No invented/stacked discounts, below-floor bargaining, automatic repricing of sent cards, or claims about refunds that did not happen. Writer wording and charged amount must agree with immutable approved terms.
- Exclude a whole bundle when any included paid asset is already owned. Distinguish free previews, paid assets, advertised cards, sent cards, unlock/access evidence, transactions and reversals. Missing ownership evidence means unknown, not unowned.
- Receive and acknowledge established voluntary tips. No proactive tip requests, promised rewards or automatic content entitlement from a tip.
- AI continues alongside staff as Jesse requested. Outside messages remain honestly attributed context. They do not become our approvals, purchases or creator facts. New relevant context invalidates stale unsent work. Explicit stop/no-contact/takeover, uncertain delivery/cost, malformed evidence and budget controls remain effective.
- Answer questions first; preserve corrections and unanswered burst questions; avoid repeated openers/emojis/pitches; use approved creator/product facts. A blocked sale should not block safe ordinary conversation unnecessarily.
- No new customs, physical goods, paid external access, timed chat packages, VIP ladders or outreach in this release.
- Onboarding within 24 hours is a measured target, not permission to declare partial history complete. The actual rate, read budget, media analysis cost and completeness must be recorded.

## 4. What has actually passed

Builder evidence on the final preparation image: 69 + 133 = 202 selected checks; actual Next BFF -> ASGI authentication middleware -> PostgreSQL integration; four TypeScript configurations; 20 browser assertions. The browser uses the actual Preparation component with synthetic fetch/minimal fixture CSS, not the production dashboard layout/build.

The reviewer independently reran 77 existing exact-image checks successfully and added three counterexamples, all of which fail. These 77 are subsets of existing coverage, not additional unique product behaviors. Full raw evidence and commands are in `/srv/projects/twin-commercial-review-20261111`.

Source comparison: 2,666 tracked files checked against the image; no missing tracked files, no extra Python files under `/app/service`, and only `.dockerignore` differs. Application code matches. The image's inherited schema/overlay labels are stale; correct them in the next release manifest.

Verified preparation protections: disabled runtime/policy, common send-admission refusal even after preparation stop, read reservation before wire, provider-family and creator/binding/epoch checks, per-account worker exclusion, apply-after-stop fencing, preserved cursor/reservations, disabled manually priced drafts, atomic/idempotent batch creation and retained legacy authority.

No production state, paid call, live message or application source changed in the review. Reviewer lab is stopped. Do not reset a shared database. Builder's final lab/checkpoint state must be read separately.

## 5. First repair: R0, the inherited conversation no-reply defect

The candidate inherits this unchanged from the deployed 659ea291 base. Preparation changes did not introduce it.

Previously verified live example, Brisbane 11 November: 18:00:55 inbound message 12024091960310 -> request `0165083a-5399-486b-9af3-36d82fb0da98` -> model call `b4f7d339-466f-4c65-81b2-10faa986850d` -> released `http_429` at 18:00:59.637. It remains open without approval/decision. The 18:13 follow-up created a new request and received a reply; it did not repair the older request.

Actual code chain:

- `service/model_transport/pilot_supervised.py:345-352`: released `http_429` is included in the uncertain predicate; lines 364-365 exclude that open work from automatic planning.
- `service/conversation_engine/model_runtime.py:209-216`: runtime can re-enter after a transport cooldown, but the scheduler does not invoke it.
- `service/model_transport/openrouter.py:206-207`: non-200 response becomes status only; diagnostic body fields, Retry-After and correlation metadata are lost.

External HTTP429 is established. Its precise external limiter/quota and enforcing party were not retained; do not claim Relace capacity, fan-message speed or a particular account ceiling caused it. Our established defects are stranded work and missing diagnostics.

Implement a narrow repair compatible with the deployed schema-0129 trial in an isolated branch/worktree, then integrate the same reviewed fix into the commercial branch. Do not deploy the unfinished commercial/schema-0132 build just to fix the trial. First check whether an exact compatible fix has since been completed elsewhere; reuse and verify it rather than duplicating ownership.

Required behavior:

1. Retain bounded sanitized status, correlation/provider codes and retry timing; no secrets or unbounded raw responses.
2. Positively classified retryable failed work gets a durable due time and bounded attempts, then returns through the existing scheduler/runtime after cooldown without another fan message. Respect Retry-After where supplied.
3. Retain unknown-outcome/cost reservations, identity holds and genuine-refusal handling. Do not make all released/unknown calls retryable. Revalidate current scope, stop/takeover, context, budgets and authorization before generating or sending.
4. Supersede obsolete requests safely when newer context or a delivered continuation exists. Do not blindly replay the already-superseded historical request into the chat.
5. Expose delayed/exhausted/held work accurately; green process readiness alone is not conversation completion.

Acceptance: actual supervised tick -> 429 -> due-time wait -> success without new inbound; restart before due; two workers; repeated 429/exhaustion; stop/takeover; correction/newer inbound; unknown cost/outcome; no duplicate send; other fan/account progresses. Direct manual calls to engine.plan do not prove automatic scheduler recovery.

## 6. Preparation repairs: R1-R3

Run `/srv/projects/twin-commercial-review-20261111/test_review_counterexamples.py` unchanged first. It was run against the exact image with no application overlay; command and raw failure log are attached/in the same directory.

### R1: provider retry timing lost

Mocked vault HTTP429 with Retry-After 120 allows consecutive `prepare_one` passes to read the page again immediately. Two page reads happened about 0.049 seconds apart in the direct-call counterexample; four requests were reserved including identity reads. Production defaults to a slower 15-second loop, but has no durable provider deadline either. Budget ceilings still hold.

Owners: `fans_api_adapter/protocol.py:109-134,234-245`; `fans_api_adapter/vault_sync.py:195-223`; `conversation_engine/preparation.py:240-244`; `onlyfans_worker.py:294-304`.

Retain bounded status/trace/retry metadata; persist due time and attempts in the existing job/attempt owners; skip identity and page I/O until due. Separate transient failures from authentication/identity and malformed-data failures. Bound retries and show exhaustion. Preserve cursor, per-account isolation, epoch/apply fencing and cumulative reservations. This importer retries too early; R0 instead strands work. Do not merge their outcome semantics blindly.

### R2: failed identity hidden from status

An identity HTTP401 raises a refusal, but prep.view still shows configured/connected=false/vault=null and no failure reason. The dashboard labels the account as awaiting verification/worker activity. The worker's generic held result is not persisted for dashboard use.

Owners: `conversation_engine/preparation.py:146-168,240-242,296-305`; `dashboard/components/onlyfans-preview/Preparation.tsx:50-55`.

Persist an account-scoped sanitized attempt result, cause, last progress, retry/exhaustion and required operator action; surface it through the existing API/BFF/component. Distinguish missing credential, authentication/identity mismatch, transient error and budget exhaustion. Permanent failures must not silently consume the entire ceiling by polling. Keep pending-command idempotency and stop/resume semantics.

### R3: old confident classification survives uncertainty

After bound metadata with confidence99 establishes proposed classification, confidence20 still returns already_categorised and retains proposed/metadata_evidence. The one-time `_auto_folders_v2` marker returns before current evidence is checked.

Owners: `vault_auto_folders.py:101-123`; `vault_ai_api.py:2937-2946,3009-3029`; `onlyfans_runtime/vault_editing.py:150-152`. Existing tests cover first pending->analyzed transition, not the later analysis.

Separate preservation of agency folder choices from validity of commercial evidence. Invalidate/re-evaluate changed confidence/content/analysis without undoing manual folders, renames, archives or locks. Uncertainty stays visibly unresolved/Miscellaneous and unsellable until review. Preserve price approvals and reject stale reviewed drafts; do not reset every folder marker. Test high->low, low->high, changed stamp, manual corrections and repeated refreshes. No automatic sale was demonstrated by this probe: sale_approved remained false and drafts disabled.

If any reproduction is contradicted by better evidence, document that before changing code. Do not implement speculative fixes just to satisfy an audit sentence. Existing protection assertions stay; disclose and justify any fixture/assertion change.

## 7. Requalify the preparation increment

Close R0-R3 with a compact table: defect -> owner -> failing counterexample -> passing repair -> positive/negative regression -> commit/image. Use one final source/image for the relevant runs; label inspected prior evidence separately. Verify normal worker execution and persisted dashboard status, not only helpers.

Correct image source/schema labels and publish one current manifest. Preserve compatibility and audit/budget rows with additive migrations when needed. Do not hide durable retry state in an unrelated free-text reason or introduce a parallel scheduler/ledger. Run affected checks on a dedicated Work Server lab with bounded resources; no laptop heavy builds or shared-lab resets.

## 8. Finish commercial completion in this order

1. **Disabled onboarding and historical coverage.** Finish factual descriptions/category review and existing conversation/purchase/access import. Keep recent catalogue selection and historical coverage independent. Reconcile events received during import, pages/cursors, duplicates and totals. Do not download every full-resolution asset just to discover ownership. Unknown coverage blocks only unsafe sales where possible; it is not a reason to fabricate access facts.
2. **Retained authority -> commercial account contract.** Existing preparation rows intentionally cannot activate. Add a compatible versioned transition retaining account identity, counters, binding, evidence and legacy pilot semantics. Do not delete/recreate preparation scopes or use a fresh budget row to bypass them.
3. **Catalogue and consistent terms.** Remove silent reliance on the old 200-row search/coverage bound; use scoped indexed/paged eligibility and a bounded relevant model shortlist. Support approved singles and explicit mixed bundles, manual category prices and approved standard/discount variants. Product approval and autonomous runtime authority remain separate. Prompts, output/edit validators, quote/grant hashes and final dispatch must read the same terms.
4. **Existing sale and purchase owners.** Normal flow is authenticated request -> accurate answer -> eligible approved product -> immutable authorized offer -> correct paid card once -> canonical receipt/history -> exact matching unlock/access and separate financial evidence -> continued conversation. Preserve predecessor/successor grant lineage. Accepted/unknown sends cannot be treated as never sent. A terms change cannot silently alter a sent card.
5. **Staff coexistence and tips.** Use account-scoped coexistence policy and existing intake/context controls. Staff messages do not become our sale grants or approved creator facts. Revalidate/cancel stale unsent work, preserve explicit holds, and never settle our offer from an outside card's unlock. Acknowledge only verified voluntary tips; replay/conflict/auth handling must preserve financial evidence.
6. **Whole-system qualification.** Prove the full sale through normal standing permissions without injecting a per-fan grant into the fixture. Include owned items/partial bundle ownership, old purchases, previews, accurate media type/duration/count/price wording, declined/reopened sales, corrections, duplicate/out-of-order events, crashes after acceptance, unknown wire/cost, retry exhaustion, stop/takeover, and staff arriving during multipart/throttle gaps. Prove ten-account isolation and bounded throughput/fairness with repeated fan/media IDs.
7. **Final release and bounded launch.** Freeze backend/dashboard/schema/config/model/profile versions, real dashboard build/BFF journey, real provider shapes and approved real-model qualification. Test populated backup/restore in quarantine and compatible rollback preserving accepted/unknown evidence. Verify installed worker/receiver/sender/supervisor composition, budget/stop controls and attribution. Then a bounded real sale, independent results review, and gradual expansion. Do not equate preparation passes with completed commerce.

## 9. Required real inputs and existing live context

Before paid preparation, obtain/reuse the approved per-creator credential reference, provider account ID, OnlyFans creator ID, read request/credit ceilings, credit cost assumptions, approved profile/boundaries and agency price/rights approvals. Confirm actual provider behavior/charging from approved evidence. Do not email plaintext secrets.

The existing trial and its host setup are separate from this offline build. Last independently diagnosed sender: commit659ea291, schema0129, Malie of-323358988, fans318022380 and601164546, DeepSeek via OpenRouter with one pinned upstream and no fallback, text-only. Approved limits in that last snapshot were100 message units shared,100/day,250 requests/reservations,400 early-stop within500 monitored credits,USD5 total AI allowance. These are dated observations/approvals, not a fresh production check and not a default budget for ten creators. The current source handoff does not prove the present trial is running.

AWS pilot root: `/srv/projects/twin-plan-c-pilot-20261109`. Existing host artifacts/guard/config and actual installed versions require authorized inspection before any later deployment. Never copy a development migration or preparation credential registry into production without qualifying the composed release.

Work Server reader access suffices to begin this handoff. GitHub write, production/AWS, provider credentials and paid-model access are separate capabilities. Use existing approved access; identify an exact missing capability if blocked. Continue independent offline work while waiting for real inputs.

## 10. Completion reporting

No unsolicited architecture rewrite, extra product policy or repeated audit churn. Work through the verified defects and existing plan. Keep one builder and one current release record. Report completed behavior, exact commit/image/schema, tests actually run, remaining evidence gaps and next concrete action. Never close an issue only because it failed on the baseline, and never describe every guarded silence as successful conversation.

The target is correct behavior with bounded recovery and clear evidence. Neither a plan nor a finite test suite can guarantee zero future errors or prevent external capacity failures. Do not promise that; demonstrate the actual outcomes and preserve the safeguards.
