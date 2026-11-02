# Moxy: full-project completion handoff for Claude

Prepared 2 November 2026. This is the handoff for the **new Moxy reproduction engine**, not the earlier R0–R9 conversation repair. Continue the existing implementation. Complete the entire scope below; the small pilot is an intermediate qualification step, not the final deliverable.

## 1. Mission and authority

Automate the process Moxy teaches its human chatters: natural conversation, creator voice, active selling, prescribed product sequences, relevant invitations, accurate captions, purchase-aware continuation and customer care. Start with Amelia and Cristina. Adding another creator must require configuration and verified account/product mappings, not another engine.

Use the three common Moxy PDFs as the foundation. Explicit creator-sheet instructions override the corresponding common instruction within their stated conditions. Where the sheet is silent, use the foundation. Jesse will attach the PDFs. Do not introduce a new selling strategy, fixed offer quotas, arbitrary filters or legacy discount rules.

Historical exchanges are execution evidence, not overriding policy. Keep compliant, prohibited and uncertain examples distinct, with context, applicable source pages and reasons. These illustrate decisions and style, not mandatory wording or word whitelists. Imported memory cannot rewrite approved creator facts, restrictions or pricing. Fan corrections update fan facts; verified transactions update ownership; conflicting historical creator claims remain untrusted.

The original consolidated requirements are preserved alongside this file as `BUILD-BRIEF-20261102.md`. This handoff updates implementation status, corrections, repository locations and completion order. It restores full-project scope after the temporary pilot-only restriction. Historical notes saying work must remain stopped or unpublished describe the earlier checkpoint; Jesse now authorises handoff publication and Claude's continuation of isolated development.

Continue routine implementation, testing and review-branch publication independently. Escalate genuine missing authority, data or access with the exact missing item and where you searched. Do not merge, deploy, activate creators, change production configuration, make purchases or send customer messages without separate authorisation. Keep paused bulk history imports and background memory processing paused. Do not expose secrets or run unbounded paid model jobs.

## 2. Exact repositories and preserved checkpoint

| Component | Retrieve / preserved state |
|---|---|
| Backend | `twinagency/twin-onlyfans-backend`, branch `codex/moxy-review-20261102` |
| Backend entry document | `docs/moxy-replacement/CLAUDE-HANDOFF-20261102.md` |
| Backend previous GitHub head | `c5fded8f92ff08a8b7f0023306ef2346681261e6` |
| Backend server checkpoint now preserved | `54020a7193b79521be3ba284ea8582b2b94d3537`, branch `codex/moxy-replacement-20261102` |
| Dashboard | `twinagency/twin-dashboard`, branch `codex/moxy-review-20261102`, commit `956741b546c9a65a77e91159c8283012885d1c28` |
| Dashboard preserved server source | `a39e34a524b74bdf3bb2e5571453e56401796864` |
| Dashboard review base | Upstream `0cab2aee9669d57e989e1a26832085308c240c81`; server archive base `c5f1915` |

The final backend publication SHA is in the companion `PUBLICATION.json` delivered with this handoff. Resolve the branch head and preserve any subsequent valid commits before editing.

**Git history warning:** the GitHub backend review is a snapshot with different ancestry from the development server. Do not force-push the server branch over it or blindly cherry-pick the entire server history. Continue from the published review branch in a separate worktree. During this handoff, all 991 tracked `service/` files were compared by Git blob hash: only the four newly preserved pilot source/test files differed from the earlier published snapshot. Those exact files are included in this publication.

The dashboard branch preserves all nine changed frontend source/test files against their verified upstream archive base. This is not proof that the archive equals the currently deployed dashboard. Recover and compare the running frontend source before final integration; preserve newer UI and onboarding work. Generated `artifacts/` are excluded.

Development server:

- SSH alias: `work-server`; host observed as `ubuntu-s-4vcpu-8gb-syd1`.
- Backend: `/srv/projects/moxy-engine`; environment: `/srv/projects/moxy-engine-env`.
- Dashboard: `/srv/projects/moxy-dashboard`.
- Source package: `/srv/projects/moxy-source`; source verification: `/srv/projects/moxy-source-verification`.
- Older handoff/package: `/srv/projects/moxy-handoff-package` and `/srv/projects/Moxy_Complete_Handoff_2026-11-02.zip`. These predate later recovery and pilot work. They are supporting evidence, not the latest code.
- Isolated PostgreSQL test wrapper: `/srv/projects/moxy-resume-test-runner.py`. It obtains disposable fixture credentials internally. Do not print or publish them.
- Existing logs: `/srv/projects/moxy-*.log` and matching `.exit.json` files.

The repository default branch is not the implementation base. Reconcile concurrent onboarding/history and original concurrency repairs by actual diff and tests. Preserve the Moxy ownership fence in `service/twin_bridge/coordination.py`; do not overwrite it with an older integration copy. Telegram, FLIRT and a general dashboard redesign remain out of scope.

## 3. What exists now

An integrated candidate exists, beyond the initial configuration-only prototype. Main backend areas:

- `service/moxy_replacement/configuration.py`, `sources/*.json`: source-mapped common/creator configuration and validation.
- `authority.py`: products, sequence and action authority.
- `writer.py`, `runtime.py`: instruction/context assembly, model decision, durable fan/event state and progression.
- `routing.py`, `delivery.py`, `campaigns.py`: legacy fencing, delivery checks and campaign admission.
- `controls.py`, configuration API and dashboard controls: mode/readiness/decision controls.
- `service/db/onlyfans_moxy_models.py` and related migrations: durable engine state.
- `service/tests/test_moxy*.py`: configuration, integration, workflow, control, concurrency, upgrade, recovery and pilot fixtures.

The code also contains selected onboarding compatibility, tenant reporting and historical-schema fixes. Preserve valid shared infrastructure. Reported confirmed-429 recovery retains the original rejection and decision, waits for the existing cooldown, uses a new bounded attempt key and stops after three confirmed rejections. Accepted/unknown delivery is not blindly retried. Authentication failure produces a visible hold. Campaign recovery remains explicitly incomplete.

The final stopped work was committed unchanged as server `54020a7`. It adds:

- `service/moxy_replacement/pilot.py` and `service/tests/test_moxy_pilot_pg.py`.
- Pilot integration in `runtime.py` and `writer.py`.
- `docs/moxy-pilot-paused-20261102.md` with the exact stop record.

Pilot scope is optional, restricted to 1–10 selected fans, selected products and a named supervisor. It blocks unattended campaigns/scheduled/fulfilment events and routes specialist services or complaints to that operator. These are **pilot restrictions**. They must not silently become the permanent full-engine behaviour or remove source-authorised remedies, campaigns and services. Check both pilot-present and pilot-absent paths, including prompt instructions and handover categories.

No further functionality was built during this publication pass. Preserving the candidate does not certify its correctness or launch readiness.

## 4. Evidence, with its limits

| Recorded check | Result | Meaning |
|---|---|---|
| Latest pilot controls | 12 passed, 65.38 s; exit 0 verified | Simulated models and delivery |
| Pilot qualification suite | 51 passed, 211.93 s; exit 0 verified | Overlaps the 12 checks |
| Ten-fan pilot fixture | 14.938 s, batches of two | Synthetic model/provider only |
| Earlier full Moxy fixture suite | 108 passed, 293.55 s | Earlier checkpoint; not a fresh all-code sweep |
| History/connection/Moxy integration | 70 passed, 253.34 s | Overlaps other runs |
| Recovery/integration and extended recovery | 17 passed and 14 passed | Overlap; simulated providers |
| Dashboard | HTTP, type, contract and 1280/390/320-width browser fixtures passed | Not the running production frontend |
| 100-fan fixtures, creators tested separately | Amelia 134.741 s; Cristina 138.650 s | Plain replies, synthetic model/provider, no approved reply pacing |

Do not add overlapping counts or infer an overall completion percentage. The earlier 108-check sweep was not repeated after every later change. Actual-model conversation quality, real selling paths and simultaneous multi-creator production capacity remain unverified. Nothing was merged, deployed, activated or sent to customers by the stopped build or this handoff publication.

The saved full conversation findings are included as `CONVERSATION-FINDINGS-20261102.md`. They report coverage of 208,334 saved messages across 30,526 threads: 2,214 two-way histories plus grouped repeated one-way sequences. This handoff did not repeat that semantic review. The report states its grouping, attribution and missing-data limitations. Its raw private datasets are retained separately, not pushed into these review branches.

The larger review adds promotional distribution, invitations, returning sequences, potential paid-service commitments, requested resends, free corrections and outstanding bonuses. It also records historical deviations from the PDFs. A single offer-frequency statistic cannot distinguish these activities. Exact PPV-to-card links were absent from that export; matching price/time is not proof. Earlier cadence filters are analysis methods, not engine policy. Maile/Mally was not in the Amelia/Cristina export.

## 5. Two immediate verification gaps

### Correct inventory and purchase checks

**Retract the earlier empty-vault conclusion.** The last zero-row audit used creator labels as account IDs. It did not establish that either creator lacks assets. Corrected checks were not completed before stopping.

| Creator | Native account ID | Creator record ID | Handle |
|---|---|---|---|
| Amelia | `of-312988353` | `d656cc83-a7c5-5990-a4dc-91f6989d3807` | `ameliabfit` |
| Cristina | `of-63665785` | `65031fca-db08-59aa-a5ac-60d17c3cc007` | `cristinacarmella` |

These are the recovered mappings, not permission to skip tenant/provider checks. Refresh mapping read-only, then inspect actual vault assets, catalogue items/scripts, description readiness, import job status, ordered sequences, prices/access restrictions, purchase links, VIP/ownership and unresolved obligations. Distinguish unimported content, failed imports, missing descriptions, missing authority and absent payment linkage. Use the PDF prices where stated; do not say all prices are missing merely because asset bindings are missing.

Prior read-only route: AWS SSM instance `i-09a9dd3bd749643a5`, region `ap-southeast-2`, profile `jesse-gpt`, container `twin-onlyfans-api-1`. Last observed native schema: `0110_recent_history`; refresh rather than assume current. Mapping evidence command: `c11fa2dc-5e7e-4e55-82a9-78686fd6ab22`. Invalid inventory command: `73dcb743-be6a-4310-a44d-e18c6aeea20b`. The prior `inspection/deploy/moxy-pilot/native_readiness_readonly.py` prototype contains the wrong labels; correct it before reuse if recovered.

Do not substitute Telegram/Stars catalogues for OnlyFans assets/prices. Locate existing authoritative mappings before asking Jesse to recreate anything. If absent, specify the exact missing sequence/asset/purchase relationship and its affected action. Complete unaffected code and fixture tests while the data is resolved.

### Actual-model access

Preserved configuration identifies OpenRouter: registry ID `openrouter/deepseek-v4.1-flash`, wire name `deepseek/deepseek-v4.1-flash`. This is a recorded setting, not a newly verified live choice or recommendation. Refresh account routing read-only and verify provider compatibility before calling it.

No authorised development key was found in the inspected environment. No production key was retrieved or used. Proposed development-only location: `/srv/projects/moxy-pilot-secrets/openrouter.env`, worker-owned, mode `0600`, variable `MOXY_TEST_MODEL_API_KEY`. This location has not been provisioned.

The stopped handoff proposed a first evaluation ceiling of USD 1, at most 20 calls, concurrency at most two, stopping on unknown cost/usage. This budget was **proposed, not approved**. Obtain authorised development credentials and budget securely. Finish the integrated evaluation runner and test with clearly labelled fixture inventory and customer transport absent; it need not wait for the full real catalogue. Do not infer real-media qualification from fixture-model success.

## 6. Finish the complete engine

1. **One decision and writing owner.** Route all automated Moxy replies, welcomes, specific invitations, paid offers, follow-ups, upsells, purchase continuations and source-authorised promotions through one owner. Preserve manual takeover and account/fan isolation. Fence every legacy writer, scheduled job and caption rewrite, including retries and cutover queues. No silent legacy fallback when Moxy is Off or fails.

2. **Active selling faithful to the sources.** Handle direct buying requests promptly. Retain item-specific invitations so clear acceptance produces the correct paid offer, never an automatic charge. Naturally develop suitable opportunities and continue the approved sequence from verified purchases and the fan's response. Include source-authorised welcome offers, promotional outreach, free previews, rewards and discounts where applicable. Do not invent a universal welcome card, random promotion timer, generic discount ladder or free-image entitlement.

3. **Rhythm and exceptions.** Preserve the sheets' actual timing rules and separately approved operational reply settings. The earlier median of three creator bubbles and 2–6 middle range is descriptive, not minutes, substantive turns, a repeat-offer rule or conversion proof. No fixed deadline or automatic send based on counts. Keep buying questions, accepted invitations and owed fulfilment open until resolved; surface stalls and their actual reasons. Respect no-push fans, ordinary-chat requests, budgets, refusal, hardship, privacy and escalation. Do not classify paid-service conversation as sales drift. Resolve unknown shift/label definitions rather than guessing.

4. **Products, descriptions and captions.** Bind real media IDs, versions, prices, format/count, available descriptions and sequence/access rules. Verify or generate internal descriptions on import/change, cache them with evidence, and expose pending/failed/unsupported states. The writer creates contextual captions from those facts and applicable Moxy instructions. Descriptions and captions are separate roles, not an excuse for competing sales writers. No invented live/custom claims, unseen video actions, scarcity, extras or unavailable services. OnlyFans already displays attachment count/price; do not impose a requirement to repeat both in every caption.

5. **Exact commercial state.** Persist invitations, offers, refusals, valid quotes, sequence position, ownership and obligations across returning sessions. Use exact provider receipts/purchase linkage; reconcile unknown outcomes before retrying. Handle rejected cards, different requested items, authorised resends, corrections, refunds/reversals and duplicate events distinctly. Preserve purchases and delivery history. Never weaken these protections to make a test pass.

6. **Services and complaints.** Complete the PDF-authorised custom/paid-service lifecycle, quote/payment route, scope, owner, deadline, fulfilment and recovery. A real owned human workflow may satisfy a required handover; a silent pause or invented availability does not. Normal complaints must support authorised remedies. Ensure pilot-only deferral does not suppress these full-engine functions. Preserve truthful identity pause/flag/handover; do not copy a human's “not a bot” statement as an AI denial.

7. **Campaign and provider recovery.** Complete audience/recipient state, source-authorised scheduling, ownership and purchase checks, interruption by replies/takeover, and campaign rejection recovery. Qualify authentication repair, rate limits, stale decisions, worker restarts and timeouts after acceptance. Existing external tools require explicit coordination; do not claim control over Infloww broadcasts without evidence.

8. **Integration, controls and scale.** Finish end-to-end media import/change, dashboard API/middleware consistency, Off/Preview/On and readiness explanations, visible owned cases, and configuration onboarding for more creators. Preview cannot send or advance live purchases. Preserve valid onboarding/history/concurrent-delivery work. Qualify multiple creators, per-fan ordering, tenant isolation, provider limits, backlog recovery and cost at a declared supported load. Do not call the two separate synthetic 100-fan runs a fleet-capacity proof.

## 7. Completion order and acceptance

Start by checking the two review heads and preserving any subsequent work. Read the attached PDFs, full brief, configuration, source mappings, findings, paused checkpoint and existing tests. Produce a compact requirement-to-code/test gap list; continue building without asking about every routine step.

First resolve the corrected read-only inventory audit and the bounded actual-model test setup. In parallel where independent, complete the missing service, media-description and campaign/recovery paths, then reconcile shared infrastructure and dashboard source. Keep unresolved authority explicit; never fabricate asset mappings, approvals, purchase proof or model results.

Run focused regressions for changed behaviour and the final relevant integration suite on the exact candidate. Existing test runner uses isolated PostgreSQL, not production. Review full conversations with the actual model across both creators, compliant/prohibited/uncertain cases, and held-out paraphrases. Test creator overrides and foundation fallback, plus a third synthetic creator added by configuration alone.

Required adversarial paths include new fan input during generation, price/catalogue changes, removed media, missed attachments, two workers, duplicate webhooks, restart after reservation, timeout after provider acceptance, human/external takeover, campaign interleaving, ownership changes and payment reversals. Verify no other sender can bypass the applicable hold or owner.

Return one finished review package: exact backend/dashboard commits, source mapping, full-scope checklist, actual versus simulated results, test commands and environment, supported load/cost, remaining external blockers if any, legacy components removed/isolated, migration/configuration diff, controlled-provider test procedure, cutover and schema-compatible disable/rollback plan. Disabling must preserve obligations and not restart a legacy writer.

The first supervised pilot can be prepared once its real scope is verified. It does not remove the rest of the project. Finish all source-required workflows and scalable configuration, and distinguish code-complete, model-qualified, provider-qualified, deployed, enabled and purchased. Real-provider tests and launch actions remain separately authorised. Do not claim “finished” merely because fixtures pass, or guarantee identical revenue or error-free AI behaviour.

## 8. What Jesse supplies and what Claude must retrieve

Jesse supplies the five Moxy PDFs. The two GitHub review branches supply the implementation and tests. This directory supplies the consolidated brief, findings, checkpoint and publication evidence. The historical report references larger private evidence archives; retrieve those from the existing authorised source package/server when contextual examples are required. Do not restart collection or make up unseen source content if an archive is unavailable.

Request only genuinely missing operational authority: secure development-model access/budget, unresolved current labels/shift definitions, exact unavailable sequence mappings, and the owner/approval of a source-required service. These are not permission to redesign Moxy's process. No further routine product-strategy decisions are delegated to the model.
