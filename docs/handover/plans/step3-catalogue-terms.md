# Step 3 plan: catalogue and consistent terms (read-only design, 2026-11-11)

Base inspected: `/srv/projects/twin-commercial-sales-20261111/backend`, branch claude/commercial-r0-r3-20261111, HEAD be7108d. Migration head is `0133_preparation_retry_state` (chain 0129b > 0130 > 0131 > 0132 > 0133). Paths are relative to `service/`. Nothing was edited or run.

## 1. Verified current behaviour

**Schema.** `CatalogItem` (db/models.py:1841-1900) has kind, label, description_for_ai, media_ids/preview_media_ids (JSON text), price_cents, tags, enabled, pricing_mode ('dynamic'|'fixed'). It has no category link, product-kind enum, bundle membership rows, discount or terms revision. 0130-0133 added: vault provenance on of_vault_sync_job; `vault_folders.single_price_cents/price_revision/pricing_*` (vault_editing.py:104-135); disabled category drafts (vault_management.py:464 `create_category_drafts`: fixed mode, enabled False, bundles need `approvedTotalCents`, audit in OfVaultProductCreate.product_json with reviewedState, re-verified by `assert_category_review` vault_editing.py:156); preparation. So single prices and bundle totals already exist as manual agency input; there is NO approved discount, NO bundle-member table, NO terms revision.
`OfEngineOfferGrant` (db/onlyfans_engine_models.py:203-228): unique (account,fan,item,terms_hash,issuance); unique successor per predecessor; lineage CHECK allows only reasons `cancelled_unsent`,`expired_renewal` (:211). `OfEngineCatalogueAlias` :323 (item+content_hash versioned, index on account,binding,normalized).

**Terms flow (the good news).** One function builds terms: `onlyfans_runtime/media_delivery.py:30 catalog_terms()`. Its dict is hashed into the grant (`commerce.py:_hash`, grant.terms_hash), stored as decision.terms_json and intent.catalog_snapshot_json, compared in `delivery.prepare` (:41-50), `delivery.media_current` (:113-137), `approval._build` (:139-181), `media_delivery.terms_match` (:89), projected by `media_delivery.project` (:117-162), shown in `offers._cards/_match` (offers.py:55-139), and matched to the unlock by `intent.price_cents` (commerce.py:566, fans_api_adapter/payment_evidence.py:140, onlyfans_runtime/commerce.py:181). So putting a `commercial` block inside the `catalog_terms` result propagates to every hash and dispatch check with no second pricing path.

**Price rules today.** media_delivery.py:42-50: `item.price_cents <= price`, fixed items require `price == item.price_cents`; below saved price is refused, so a discount cannot exist. 12 callers of `catalog_terms`: commerce, engine (`_verified_purchases`), catalogue, pilot_scope, native_sales, campaigns, text_delivery, launch_operations, launch_engagement/{readers,service}, onlyfans_read/workspace.

**The 200-row bound and single-product assumptions.**
- catalogue.py:18-19 `_MAX_ITEMS=200`, `_MAX_ALIASES=1000`. `_catalogue_version` (:71-99) selects 201 items/1001 aliases and returns None (coverage unavailable) above that, and None if >7000 media ids (:91). `search_scoped` (:199-203) loads at most 200 enabled items ORDER BY id, silently downgrades coverage to 'incomplete' above 200 and ignores the rest; per item it runs `catalog_terms` + `content_hash` + an alias query (N+1, :205-231). Callers: engine.py:410,576,651, memory.py:70.
- engine.py:~196 `_verified_purchases`: `limit(201)`, returns None above 200 (second silent bound). `_offer_grants` engine.py:117-142: 6 grants, 5 used. `offers._match` limit 50 / `_cards` limit 10 (reported as incomplete, acceptable).
- vault_management.py:36,571 `CATALOGUE_BOUND=2000` duplicate check (refuses; acceptable but replace with media-hash lookup). vault_editing/folders 200 (editor, out of scope).
- Single configured product: pilot_scope.py:94-102 (`config['product']` id/price/media/preview); `_product_current` :264-276 (checked :571, :691); commerce.py:225 (successor needs `config['product']['id']==prior.item_id`), :440 `prepare_pilot_offer` uses the one product; :452-455 any earlier grant for the item at a different terms hash => `offer_authorization_terms_need_review` (a terms change can never mint a new grant today); engine.py:145-170 `_price_information` pilot product only; journey.py:219-250 `pilot_purchase_scope` regex only for config product; approval.py:172-179 `pilot_product_scope_changed`; journey.backfill_complete reads singleton `OfEnginePilotScope(1)` (:292). These belong to the scope-transition planner (0134); Step 3 only needs a stable `commercial_scope(s, runtime)` predicate from it.

**Prompts and validators prohibit discount/bundle.** prompt_provider_a_original.py:31-33 ("No discounts, cheaper options, bundles or payment plans"), :150-158 APPROVED OFFER/CONTENTS/INFORMATIONAL PRICE lines; model_output.py:149-151 `_DISCOUNT` (discount, cheaper, half price, % off, bundle, payment plan...), applied at :300 (`_writing_problems`) and :483 (`edit_problems`, the human-edit check); allowed prices :527; claims.py:69-71 (prices from offer_grants + price_information), :461-495 `check_plain_text`; writer_context.py:311 `pick_offer` (video/picture routing only), :581 price_information filtered by `may_offer`.

**Ownership.** `commerce._unowned` (:72-86) = `assert_unowned` (media_delivery.py:100: hero = non-preview overlap, plus `held()` and bounded read `owned_media_in_session(limit=10000)` that Refuses rather than truncating) then whole-set overlap incl. previews and provider-unlocked media. A bundle is therefore already excluded if any member is owned. Unknown handling is partial: read overflow, `held()` (unlinked paid purchase), unresolved chargeback (`_verified_purchases` None), `backfill_complete`. Gap: paid messages/unlocks with unmapped media are not treated as "unknown for every candidate".

## 2. Data model: `0135_commercial_catalogue_terms` (down_revision `0134_*` placeholder; file `20261111_NNNN_commercial_catalogue_terms.py`)

All additive. Legacy items have no terms row and keep today's behaviour byte for byte.
1. `of_catalogue_terms` (account_id, item_id FK catalog_items RESTRICT, revision int>=1; PK account,item,revision): `status` approved|superseded|withdrawn; `product_kind` single_photo|single_video|bundle_photo|bundle_video|bundle_mixed; `standard_price_cents` CHECK 300..20000; `discount_price_cents` NULL or 300<=d<standard (CHECK); `price_source` agency_category|agency_bundle_total; `category_folder_id`, `category_price_revision` (required for singles, must equal vault_folders values at approval time); `media_count`, `paid_photo_count`, `paid_video_count`, `preview_count` (media counts, distinct from product count); `approved_by`, `approved_at`, `binding_id`; `snapshot_json` + `snapshot_sha256` (canonical frozen record incl. members, prices, category revision, reviewedState hash). Partial unique index `(account_id,item_id) WHERE status='approved'` (one live revision). Trigger like 0130's: rows immutable except `status` approved->superseded/withdrawn; no delete. CHECKs: single kinds have 1 paid member and no preview; bundles >=2 paid.
2. `of_catalogue_terms_member` (account_id,item_id,revision,media_id,role paid|preview,kind photo|video; PK all four) with `ix_terms_member_media (account_id, media_id)` for reverse "which products contain asset X" and set-based owned exclusion. Same immutability trigger.
3. `catalog_items.search_tsv` generated tsvector (label, description_for_ai, tags) + GIN `(search_tsv)`; index `ix_of_catalogue_terms_live (account_id, product_kind, item_id) WHERE status='approved'`.
4. `of_engine_offer_grant`: replace lineage CHECK to also allow `terms_revision` and `discount_variant` (drop/recreate named constraint, same pattern as 0127); add nullable `terms_revision int`, `terms_variant varchar(16)` (informational; the hash stays the authority). No new grant tables.
5. downgrade refuses if any terms row exists (as 0131 does).
Immutable approved-terms snapshot: `catalog_terms()` adds `"commercial": {"v":"commercial-terms.v1","revision":n,"snapshotSha256":h,"kind":...,"variant":"standard"|"discount","standardCents":..,"discountCents":..|null,"counts":{items,pictures,videos,previews},"priceSource":...}` only for items with a live terms row. `terms_hash` of the grant covers it; the terms row itself is immutable evidence.

## 3. One approved-terms read API

`onlyfans_runtime/catalogue_terms.py` (new, imported by media_delivery to avoid cycles):
- writes: `approve_terms(store,tenant,account,item_id,*,standard, discount=None, command_key, actor)` (agency `manage` capability, actor not `worker-import:`; singles: standard must equal live category price revision; bundles: explicit approved total; no history/AI price input exists in the signature) and `set_discount(...)` which creates revision+1 and supersedes. `create_category_drafts` also writes revision 1 through the same function (approval is not enabling and is not account activation).
- read: `catalog_terms(session, account, item_id, *, price_cents=None, lock=True)` stays THE entry point; commercial branch: price must equal standard or (if present) discount; `item.price_cents` must equal standard (else "catalogue price differs from approved terms"); pricing_mode must be fixed; variant derived from price. Legacy branch unchanged (incl. the below-saved-price refusal at :42-50).
- `commercial_facts(terms)` returns the display facts (variant, prices, kind, counts, isBundle, discountApproved). It is the only function prompt, validators and claims call.

Edits per consumer:
| consumer | edit |
|---|---|
| media_delivery.catalog_terms/terms_match/project | commercial branch; `project` re-asserts snapshot variant price is in snapshot approved prices; terms_match fails if live revision != snapshot revision |
| commerce.verified_terms/_approve/_successor | no signature change; hash covers commercial block; add `_terms_successor` (section 5) |
| engine._offer_grants | counts and variant from `commercial_facts`; emit `variant`, `discountApproved`, `standardCents` |
| engine._price_information | per shortlisted eligible item (max 3) when fan asked price; pilot path unchanged for legacy |
| prompt_provider_a_original | rules keyed on `ctx.terms_mode` (see below) |
| model_output._DISCOUNT (:149,300,483) | split into always-banned (`% off`, percent, half price, payment plan, pay later, knock off, "for less") and gated words (discount, cheaper, lower price, bundle) allowed only when the turn's offer facts say discountApproved / isBundle; invented amount not in {standard, discount} fails via claims |
| claims.grounding_from_snapshot (:69) | allowed prices = {variant price, standard price only when discount offered}; quantities from per-card `counts` not `len(mediaIds)`; keep product-count and media-count separate |
| model_output.edit_problems | same gated helper, same ctx.snapshot facts |
| approval._build, delivery.prepare, media_current | unchanged code paths; the new block is inside `terms`; pilot-product check at approval.py:172 replaced by "terms live and matches scope" via 0134 API |
| content_management._v1/_v2_guard (:146-170) | refuse price/enabled edits that would diverge from live terms on commercial items |
| offers._cards/_match | label/price already from frozen terms; add kind/variant to card for the prompt |
| native_sales, campaigns, launch_*, workspace | read through `catalog_terms`; commercial items remain excluded from dynamic native quote paths (assert pricing_mode fixed) |

Prompt legacy preservation: keep `RULES` and `RULES_TEXT_ONLY` bytes unchanged; add `RULES_COMMERCIAL` chosen when snapshot terms_mode is `commercial.v1` ("only the APPROVED OFFER, at the exact price the application adds; a lower price exists only when the offer line says DISCOUNT APPROVED; never invent a discount, percentage, bundle or cheaper option; state counts only from CONTENTS"). Bump `name` version so prompt_sha is distinguishable; legacy rows get the identical prompt.

## 4. Selection design

New `conversation_engine/catalogue_select.py`, used when `commercial_scope()` is true; legacy `search_scoped` and `_catalogue_version` stay (pilot_scope.py:152-165 relies on legacy whole-catalogue semantics; they keep their bounds and refuse rather than truncate). 
1. Interpret request (kind photo/video/bundle/mixed, "cheaper" flag, tokens) with a small deterministic function; the model never sees the vault.
2. Owned set once: `owned_media_in_session(limit=10000)` plus provider unlocks; overflow or unmapped paid evidence => `ownership_unknown` => no candidates (state 'unavailable', reason ownership_unknown), never "unowned".
3. Keyset-paged eligibility SQL (25 per page, max 8 pages): live terms row JOIN enabled non-script item, `product_kind = ANY(:kinds)`, `NOT EXISTS (member WHERE media_id = ANY(:owned))` (whole-bundle exclusion, preview rule section 6), `search_tsv @@ query OR item_id = ANY(alias_hits)`; alias hits via indexed `normalized = ANY(n-grams)`. Order by ts_rank desc, item_id. For each row run `catalog_terms(lock=False)`, `sale_hold`, `_real_catalogue_terms` provenance; stop at K=5 verified (informational K=3). Page cap reached => coverage 'incomplete' and `truncated`, never confirmed_none.
4. Output: `{state, items:[{itemId,label,terms_hash,variant prices,productKind,counts}], productCount, mediaCount, coverage, truncated}` deterministic (it feeds `references_fingerprint`, memory.py:35; draft invalidation relies on it).
5. Coverage hash `_catalogue_version_v2`: server-side `md5(string_agg(... ORDER BY id))` over (item id, updated_at, enabled, live terms revision), alias count/max id, binding; no row transfer, no None at 201. Old coverage objects hash differently => 'stale' (safe).
6. `_verified_purchases` rewritten as one SQL: live items whose paid members are all in owned set (member table), paged, no 200 cap.
7. Counts: `productCount` (cards) vs `mediaCount` (assets) always separate; only `counts` of the offered card are quotable.

## 5. Variants, lineage, no-reprice

- Variant is chosen by application code (`price_intent.asks_cheaper(message)`: explicit ask only, e.g. "cheaper", "discount", "any deal"; "too expensive/no" stays a decline per Section 7). Standard grant: terms variant standard. Discount grant: price = discount_price_cents, only if live terms have one AND no card for that item (accepted/unknown/dispatching) exists for the fan AND no standard grant has a dispatch trace. Model only sees one APPROVED OFFER line (the discount one) with "DISCOUNT APPROVED"; the amount is never model-chosen.
- Terms change (new revision or variant): `_terms_successor(prior, new_terms)`: allowed only if prior unused or `_never_dispatched` proof holds (commerce.py:183); predecessor retained; new row issuance+1, reason `terms_revision`/`discount_variant`, authorization_json records old/new hashes and revision; unique successor constraint serialises racing preparers. If prior intent is accepted, unknown, dispatching or has wire evidence: no successor, no new card (existing-card path), operator hold for unknown. Replaces the dead end at commerce.py:452-455 only for commercial scope.
- A sent card is never altered: ContentOffer.price_cents, intent.price_cents, intent snapshot are frozen at projection (media_delivery.py:139-147); unlock matching compares the unlock amount with intent.price_cents, not with live terms. New revision cannot change those rows (triggers on terms rows + no code writes to them).

## 6. Ownership (requirement d)
Single `ownership_gate(s, account, fan, terms)` used by `commerce._unowned`, `text_delivery` and selection: refuse if any paid member owned; refuse if `ownership_unknown` (read overflow, unresolved reversal/pending unlock, `held(fan)`, `backfill_complete` false, paid message/unlock with unmapped media). Keep today's stricter engine rule (previews overlapping owned media also exclude); document it in code. Singles inside an excluded bundle remain individually evaluated.

## 7. Decisions for the owner (recommendations)
1. Discount trigger: explicit "can you do cheaper" only; "too expensive/not now" stays decline and pauses pitching. Recommend yes.
2. Preview semantics: keep engine strict (any overlap with owned excludes). Recommend yes (safe, already tested).
3. Paid evidence with unmapped media blocks all offers to that fan until an operator links it. Recommend yes (spec: unknown is not unowned); cost is lost sales for staff-sold fans.
4. Bundle total below sum of member singles is allowed (agency intent) with a dashboard warning only. Recommend yes.
5. Approval authority: any agency actor with `manage`; no second approver. Recommend yes.

## 8. Risks and tests (failing counterexample first, then fix)
New file `tests/test_commercial_catalogue_terms_pg.py` (+ `tests/conversation_engine/test_commercial_terms_wording.py`, `tests/test_commercial_terms_migration_pg.py`). Each test is written and shown failing on be7108d before its fix.
- (a) 2,500 enabled items, target at id 2,300: found; coverage not None; query-count budget (event hook) independent of N; shortlist <= K; bundle of 6 media => productCount 1, mediaCount 6, and a reply claiming "6 products" fails; page-cap => incomplete, never confirmed_none. Legacy: >200 items on a pilot account still refuses (golden).
- (b) approve each kind (photo, video, photo bundle, video bundle, mixed); `worker-import:` actor, history/AI price, missing bundle total, discount >= standard, <300, second discount (stack), percentage text, invented amount all refused; discount used only on explicit ask; existing sent card + "cheaper" => reply only, no card; sent card price unchanged.
- (c) parametrised mutation test (price, discount, members, revision): prompt lines, `proposal_problems`, `edit_problems`, claims grounding, grant hash, `approval._build`, `delivery.media_current`, `terms_match` all agree or all go stale; fixed price differing refused; override below saved price refused unless equal to approved discount at same revision.
- (d) bundle with one owned paid asset excluded; unknown cases (read overflow, pending unlock, unmapped paid message, incomplete backfill) offer nothing; sibling single still offered.
- (e) terms change on unsent grant => successor with predecessor and reason; accepted/unknown/dispatching => none, existing-card path; two concurrent preparers => one successor; sent-card rows byte-identical after revision; unlock at old price still matches.
- (f) legacy golden: unchanged prompt bytes, pilot snapshot digest and `terms_hash` for items without terms rows; text-only and pilot restrictions still reject bundle/discount text; migration upgrade/downgrade on populated pilot row.
Must stay green: tests/test_engine_catalogue_pg, test_engine_offer_pg, test_onlyfans_fixed_price_pg, test_plan_c_product_coverage_pg, test_plan_c_grant_lifecycle_pg, test_plan_c_grant_migration_pg, test_commercial_category_drafts_pg, test_commercial_vault_pg, test_commercial_full_path, test_plan_c_informational_price_guards, test_plan_c_price_answer_pg, test_plan_c_paused_price_pg, test_plan_c_card_history_rendering, test_plan_c_truthful_media_counterparts, test_plan_c_text_no_offers_pg, test_plan_c_stage5_text_only_pg / writer_pg, test_plan_c_v3_stage2_*, test_plan_c_deletion_proof_pg, test_plan_c_final_counterexamples, tests/conversation_engine/{test_claims_semantics,test_product_wording_*,test_model_units}.py, test_requested_discount_policy.py (legacy native discount lane is not reused).
Risks: (1) regex gating is leaky, so prompt hint plus validator plus structured `price_claims`; discount wording only valid when the offer facts carry the flag. (2) Shortlist churn invalidates drafts via references_fingerprint, so keep it deterministic and id-based. (3) Item edits bump updated_at and so stale grants; acceptable (safe), surface in dashboard. (4) Dependency on 0134 scope API and removal of the `OfEnginePilotScope(1)` singleton in backfill_complete; coordinate before C6. (5) Migration touches a lineage CHECK on a hot table; apply in short lock window.

## 9. Ordered commits
1. Tests only: failing counterexamples a-f (marked, evidence log of failure on be7108d).
2. Migration 0135 + models (+ migration/downgrade test).
3. `catalogue_terms.py` approve/set_discount; `create_category_drafts` writes revision 1.
4. `catalog_terms` commercial branch + `commercial_facts` (legacy goldens green).
5. `ownership_gate` + unknown handling; bundle exclusion tests.
6. `catalogue_select.py`, coverage v2, `_verified_purchases` rewrite, wiring in engine/memory behind `commercial_scope()`.
7. Variant selection (`price_intent.py`), `_offer_grants`/`pick_offer` bundle+kind routing.
8. Terms successor lineage (needs 0134 scope API) in commerce.
9. Prompt `RULES_COMMERCIAL`, model_output/claims/edit_problems gating.
10. Dispatch/approval/content_management/dashboard-read consumers; project() checks.
11. End-to-end no-injected-grant sale test, ten-account same-fan-id isolation, query-count budget; full suite run.
