# Rundown for GPT: OnlyFans repair R0-R8 + Moxy workflow engine (written 2 Nov 2026 ~01:30 AEST)

Read this first. Everything is LOCAL-BUILT and now on branches only. Nothing is merged to the release branch, deployed, migrated, activated or run against production. No real ids, fan data or creator personas are in this repo (it is public).

## 0. Branches to pick up (all on origin)

| branch | what | tip |
|---|---|---|
| `claude/of-final-integration-20261101` | R0-R8 merged + integration fixes + PROGRESS.md | a6b93bd |
| `claude/moxy-workflow-engine-20261102` | integration + Moxy workflow engine (service/moxy_workflow) + tests + shadow adapter + this file | see git log |
| `claude/of-final-eval-20261101` | offline evaluation harness (scripts/onlyfans_eval) + 20 tests | 64b6e80 |

Base start point: 6807b6e (= 2f1a397 + R0 read-only tools). PRs #75 (history capacity) and #76 (conversation integrity) were merged in first.

## 1. What the 8 hours were

Jesse's consolidated brief rev 2 + R5 clarification = stages R0-R8, run on one integration branch, no parallel authorities, reuse existing owners. Plan: `of-texting-handoff-2026-11-01/OF-R0-R9-EXECUTION-PLAN-2026-11-01.txt` (not in repo; has real ids).

### Stage status (honest)

| stage | state | notes |
|---|---|---|
| R0 baseline | branch built; **live snapshot NOT run** | needs operator on the prod host (D1). Live parity unverified. |
| R1 hold policy, detectors, operator cases | done + tested | one shared policy used by both planes; safety detectors; durable case table (migration 0105); external-sender observation |
| R2 complete answers | done + tested | OnlyFans-scoped switch, repeat detection, bounded repair, validator on every send path, no forced "?" on EXPLORE |
| R3 facts / attachments / identity | done + tested | no time expiry on creator facts (PR #67 kept); attachment state; correction merge |
| R4 inventory fairness + readiness | done + tested | bounded starvation of read lanes (migration 0106, idempotent), ONE product-readiness function, stored substitution agreement |
| R5 active selling | **partial** (merged) | per-turn action decision, lower/standard/active controls, invitations + short "yes", decision log, first-offer benchmark tool (migration 0107) |
| R6 canonical quote / lifecycle | **partial** (merged) | quote columns + obligation id + duplicate-payment guard, wording gate on deadlines/refund claims |
| R7 promo intent | done (with R3) | per-message promo classification |
| R8 timing / capacity | done (synthetic only) | turn-timing record; oldest-need-first start order; released-not-failed on cancelled send waiter |
| Eval harness | built; synthetic run done; **real-model quality score NOT run** | needs model spend (~$1.2 for 3 gens, ~$2.0 for 5) and an independent scorer |
| R9 release package | PROGRESS.md status table only | runbook, config diff, abort criteria not yet written |

### R5/R6 still NOT done (the agent was stopped before it started this)
1. `quote.price()` is not yet the single price writer: the native dynamic quote and the upseller still compute their own.
2. Lifecycle states between awaiting_payment and provider_accepted are not written by the intent/attempt code; only reconciled is.
3. `open_custom_case` is not called by the engine; R3's operator-case port has no production sink registered.
4. Not done: revalidation of edited/retracted messages, stale-veto audit, PR #76 evidence tests through real ContextualAuthority, incident-fixture replay. No F-row is marked closed.
5. Decision needed: a recent decline of the same media now suppresses a contextual card in shadow and live. Confirm that is wanted.
6. Migration 0107 adds a partial unique index on `content_offers`: needs a lock-time review before any release.

## 2. Integration fixes made while merging (all with tests)
- Schema chain: 0106 re-chained after 0105; 0106 column adds made idempotent. Head is now `0107_active_selling_quote`.
- `pack_claim._DELIVERY_PROMISE`: subjectless "sending you a photo" now held (exposed when R2 dropped the forced "?").
- R1 `safety_detect`: single-quote stripping was eating contractions, hiding "i've hurt myself and i'm scared". Added overdose / pills / "no point in living" / "don't want to be here anymore" / consent-with-adverb phrases. Found by the eval harness. Regression + nearby-legitimate tests added.
- `text_delivery`: auth-failure check read `.reason` on `store.Refused`, which has none (R1 dispatch refusal crashed the campaign-controls path). Fixed.
- Test fixtures updated for deliberate contract changes (no assertion removed): R4 readiness needs a version (`content_hash` or `updated_at_of`) and a meaningful description; shelf check reads the offered set twice; R2 validator holds tag-shaped text on every native send path; schema-head asserts.

## 3. Test state (be careful, partly unverified)
- Targeted suites for every touched area pass (contextual 122, conversation policy/holds 124, active selling 318 incl. quote/benchmark, requested caption 21, campaign controls 6, runner 262 checks, Moxy 207 + adapter 6, eval harness 20).
- **The FULL suite (269 files) was never completed on the final merged tree.** Two partial sharded runs reached ~115 files; remaining red files were either load timeouts (pass alone) or already failing before the merge: `test_native_pilot_context`, `test_native_pilot_expiry`, `test_native_pilot_storage_pg`, `test_native_pilot_load_pg`. The plan's section-4 triage of those baseline failures is still owed. Run the full suite once, sequentially or in shards, with a disposable Postgres (`ONLYFANS_TEST_PG_URL`), `PYTHONPATH=.` from `service/`; suites are mixed pytest and script style (exit 5 = no tests ran = run the file as a script).
- Egress isolation test (`test_onlyfans_egress_isolation`) and Twin-bridge tests were not run; plan says release-blocking until explained.

## 4. Open risks and decisions for Ronan/Jesse
- **R4 shelf risk (check live, read-only):** product readiness refuses any vault item without a version (`content_hash`/`updated_at_of`) or a meaningful description. If live rows lack them the pack/contextual shelf goes empty. Must be checked against live data before any rollout.
- D1 R0 live snapshot (operator). D4 model spend for the eval. D5 PR #69/#70 merge-or-reference.
- R8: after a 401 the account is blocked 300 s and a reconnect does not release it (left alone, touches auth).
- Eval findings: 11 promo cases pass the gate because production uses cross-fan evidence the case file lacks; rubric expects a reply on distress but R1 policy holds all automation (needs a decision); the worker route holds any plain-text price so priced offers cannot be fully exercised offline.
- R8 numbers are synthetic, one process on SQLite. Do NOT quote them as supported production load. (Synthetic: 100 fans on one creator, gap mode 9% within 180 s vs concurrent mode 100%; chatty-fan wait 1148 s -> 88 s after fix.)
- Rules in force: no merge to release, no deploy, no prod DB, no account activation, no customer sends. Ronan's rule: no email to Jesse unless Ronan says so.

## 5. Moxy workflow engine (new, 2 Nov, branch claude/moxy-workflow-engine-20261102)

Ronan's ask: build Moxy's "simple, step by step, less-AI" workflow engine, swappable personality, locally on a branch.

What it is (`service/moxy_workflow/`):
- Rule-first and deterministic: **code picks every step; no model decides anything.** Optional rephrase hook is off by default and cannot change any number.
- `persona.py`: one JSON per creator (voice, emojis, pet names, never-say, approved facts, content level, sexting policy, customs min/VIP-only, price table with keywords and VIP-only flags, hard NOs, VIP vault levels, label rules, pacing). Strict validation. A null price is never quoted.
- `playbook.py` + `voice.py`: reply templates per scenario with tokens, rendered in the persona's voice (emoji off, no elongation, no em/en dashes, VIP casing kept).
- `classify.py`: rule-based scenario detection for Moxy's 20 Response-Playbook scenarios, content/rating/custom asks, yes/no, plus escalation triggers (leaks, threats, stalking, real-world details, crisis, minors). Reuses the shared `native_pilot.safety_detect`.
- `engine.py`: per message: gates (escalated, do-not-engage label, shift-skip label, talker throttle) -> safety (escalate = NO reply; chargeback = one firm reply then escalate; 3 boundary pushes = escalate) -> playbook reply -> selling. Selling is scripted: an open invite accepted by "yes" sends that exact offer; a direct ask offers the matching priced item (VIP-only items refused to non-VIPs); otherwise after enough rapport it invites the NEXT vault PPV in order. Vault: 4 PPVs per level, no skipping, all 4 unlock the next level, last level = VIP with the "Name[VIP:m/d/y]" operator note. Already-bought and declined items are not re-pitched.
- `adapter.py`: SHADOW-only lane. Reads `moxy_workflow` block in the account AI config (mode off/shadow/live, persona_path, playbook_paths), replays purchases from vault_sends, records every Decision in the existing `of_sales_decision` table (stage "moxy", sent=false). Live raises `LiveNotEnabled`. No send path exists.
- `cli.py`: local fan simulator (`python -m moxy_workflow.cli --persona P --playbook B`).
- Tests: 207 engine tests + 6 adapter tests, all passing.
- Real configs (Cristina, Amelia, base playbook) are OUTSIDE the repo in `moxy-workflows/engine-config/` on Ronan's PC; they need to be handed over separately.

### How much better is it? (honest)
- **No measured comparison exists.** We have not run the old AI chat and the Moxy engine on the same fans. The `BEFORE-vs-NOW` pages in `moxy-workflows/mocks/` are hand-written mock-ups, not evidence.
- What is true by construction: it cannot invent a price, skip a vault step, sell VIP-only items to non-VIPs, discuss customs with non-VIPs (Amelia), or answer an escalation thread; it follows Moxy's own scenario replies; identical input gives identical output; it is cheap (no model call) and auditable (each decision has a scenario, reason and notes).
- What it cannot do: free-form conversation. Small talk is a short rotating set of lines; anything the rules do not recognise falls into small talk or a missing-template note. The classifier misses phrasings (known: "am i talking to an agency", "this isnt her", "how much is a rate"). The engine does not yet enforce `sexting`, `content_level`, `facts` or `phrases`. Joke threats ("my boss will kill you lol") still escalate (safe side).
- Needs from Moxy before it can be used: **real VIP vault prices/labels/level count** (we only have "use her scripts"; configs hold null prices so nothing is quoted), Cristina's shift hours/timezone for the clock-emoji rule, rating prices, the approved fan-facing facts, and agency sign-off on the honest-identity line (Protocol A2 says "not a bot", which is false for AI).
- To go live: a send path through the existing sales/delivery guards, a mode gate like contextual's, wiring into ai_chatter/native_pilot, vault ids matching `media_id` strings, fan state persisted in the DB instead of JSON files, and a real side-by-side evaluation (shadow first).

## 6. Suggested order for GPT
1. Run the full suite once on the integration branch; triage the 4 baseline failures (plan section 4).
2. Finish R5/R6 items 1-4 (the quote single-writer and lifecycle writes are the money-critical ones).
3. Check R4 shelf risk against live vault rows (read-only).
4. Write R9 package; get D1/D4 from Ronan; run the real-model eval and score independently.
5. Moxy: get the missing vault data, run shadow beside the current chat, compare, then decide on live.
