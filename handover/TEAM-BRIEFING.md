# Team briefing (condensed, 2026-11-11) — the engine repos

Source: the team's "Engine repos: briefing and rules for AI coding agents". Cut ~55 %; spirit kept. Every rule below has a test or check in §7, not a prompt. Full original: ask the team.

## 1. What it is
Twin runs AI chat clones for creators. This is the OnlyFans conversation + sales engine: fan message in → AI reply → (if approved) send + sell content cards via FansAPI. A two-fan live trial ("Plan C pilot") runs on one creator. A "commercial" build is being added (onboarding, vault import, manual pricing, bundles, purchase matching, ten creators on one codebase).
Two things must stay true: **nothing goes out unless every existing safety check passes**, and **one creator's data never leaks to another**.

## 2. Repos, branches, what is live
| repo | start from | note |
|---|---|---|
| twin-onlyfans-backend (Python/SQLAlchemy/Postgres/Alembic, engine under `service/`) | `claude/commercial-r0-r3-20261111` (d10e065) | **no `main`**; default `codex/onlyfans-of-b1` is ~600 commits behind — never base on it |
| twin-dashboard (Next.js/TS; OF screens under `components/onlyfans-preview`, `app/api/onlyfans-*`, `lib/onlyfans`) | `claude/commercial-dashboard-r2-20261111` (95ca2d9) | push to `main` = prod deploy |
Reference only: `claude/plan-c-deletion-proof-20261111` (659ea29) = exactly what the live trial runs, schema 0129, text only, no media, no sales — never build on it; `claude/plan-c-r0-retry-20261111` = its small fix; `codex/commercial-sales-*` = earlier reviewed baseline.
LIVE = the trial on 659ea29. NOT LIVE = everything on `claude/commercial-*`; going live needs the owner's explicit approval. Passing tests ≠ ready.

## 3. Scope
Edit only: backend `service/onlyfans_*`, `fans_api_adapter`, `conversation_engine`, `model_transport`, `db` (with care), `tests`; dashboard `components/onlyfans-preview`, `app/api/onlyfans-*`, `lib/onlyfans` + their tests. Nothing else (Telegram clones, revenue, other repos).
The team's own Claude is actively editing: `model_transport/pilot_supervised.py`, `conversation_engine/{model_runtime,model_ledger,model_client,preparation}.py`, `fans_api_adapter/{vault_sync,protocol}.py`, `vault_auto_folders.py`, `vault_ai_api.py`, catalogue/pricing/scope-transition code, `db/schema.py`, `db/migrations/**`. Touching these: say which files first, expect conflicts.

## 4. Hard rules
1. Never push `main` (dashboard). 2. Never force-push or rewrite `claude/*` or `codex/*`. 3. No SSH, deploys, AWS, real model/provider calls — offline, mocks, throwaway DB. 4. Never delete/reset/restart a shared DB or container. 5. No secrets, cookies, tokens, session files, real fan messages in commits; synthetic data only. 6. Never weaken a send-safety check, budget cap, ownership check or stop control to pass a test. 7. Changing an existing assertion → say so + why in commit and report. 8. No "done/fixed/ready" without a test you ran; say what you did not run.

## 5. How the engine works
Webhook (signed) → ingress row → request → scheduler tick (`pilot_supervised.py`) → `worker.turn()` → `engine.plan()` → `ModelRuntime.propose()` through the **model ledger** → reply validated → approval + decision rows → delivery intents → sender → receipts.
- Ledger `of_engine_model_call`: every model call reserved before the wire; states `reserved, calling, settled, released, unknown`. `released` = proven zero-cost refusal. **`unknown` = may have been charged: never retried automatically, cost stays reserved.**
- Scheduler holds anything uncertain. Only the existing sender path may send; stop, takeover, no-contact, unknown-delivery and budget holds always win.
- A new inbound message supersedes older unsent work for that fan; stale drafts never go out.
- Isolation: tenant → account → binding → connection → fan. Same fan/media id in two accounts never shares context, prices, purchases or jobs.

## 6. Migrations + recent fixes (do not undo)
Chain: `0129_reconnect_receipt → 0129b_model_retry_due → 0130_commercial_vault_provenance → 0131_manual_category_prices → 0132_account_preparation → 0133_preparation_retry_state`; planned `0134` scope transition, `0135` catalogue terms, `0136` history coverage — no other numbers. Trial upgrades to 0129b only until the commercial release is qualified. Additive only; downgrade must refuse to destroy evidence. `db/schema.py` holds HEAD, revision rank, pinned trigger hashes → new migration = update these + every head-pinning test, list pins in the commit.
Fixed: **R0** model 429/408/5xx/not_sent no longer sticks forever: released refusal → durable `retry_not_before` (Retry-After honoured), scheduler re-selects, 2 attempts then human; 400/401/403/404/422 → human at once; unknown never retried. **R1** vault importer honours Retry-After, due time + attempt count, gives up visibly after 5. **R2** identity-check 401 is stored and shown (cause, next retry, action); permanent failure stops polling. **R3** classification recomputed every pass; operator folders/locks/prices untouched.

## 7. Product rules → the tests that cover them (ours to build, not prompts)
| rule | test / check (where) |
|---|---|
| nothing sends unless every safety check passes; stop/takeover/no-contact/unknown-delivery/budget always win | `send-authority` unit + slop `kill-switch`: flip each hold mid-run → 0 sends in 5 s |
| `unknown` ledger state never retried, cost stays reserved | unit on `model_ledger`: inject unknown → no second call, reservation intact; slop invariant `unknown_retries == 0` |
| released refusal retries ≤2 then human; 4xx → human at once | unit matrix 429/408/5xx/not_sent/400/401/403/404/422 → expected path |
| new inbound supersedes unsent work | slop `inbox-storm` variant: second message mid-draft → first draft never delivered |
| tenant isolation | slop seed two accounts with the same fan + media id → no shared context/price/purchase/job rows (SQL assertion) |
| migrations additive, downgrade refuses to destroy, head pins consistent | CI: alembic upgrade head on empty PG + downgrade one step refuses; `schema.py` HEAD == alembic head; pin test |
| trial stays ≤0129b | check: branch `plan-c-*` migration head ≤ 0129b |
| manual category prices only; uncertain → Miscellaneous, unsellable | unit: no price without operator row; unknown category → unsellable flag |
| 1,000-item window ≠ ownership; unknown ownership ≠ unowned | unit: item outside window with purchase evidence stays owned; missing evidence → `unknown` |
| singles + approved bundles only; bundle excluded if any paid item owned; no invented discounts/stacking/repricing of sent cards | unit matrix on card builder; slop invariant `price_changes_after_send == 0` |
| tips acknowledged only, never requested | reply validator test: outbound text matching tip-request patterns → rejected |
| staff messages are context, never approval/purchase; new context cancels stale AI work | unit: staff message → no approval row; pending AI draft cancelled |
| no customs, physical goods, external access, timed packages | validator denylist test |
| no OF credentials/cookies in logs or fixtures | gitleaks + log-scan check (11h sweep) |
| no assertion weakened silently | `scripts/ci/test-guard.sh` (section 10) |
Each row = one `features.json` entry; red first, then green.

## 8. Running tests
`_pg` tests need Postgres: `docker run -d --name my-pg -e POSTGRES_HOST_AUTH_METHOD=trust -p 127.0.0.1:55432:5432 postgres:16 -c fsync=off -c max_connections=300`; `ONLYFANS_TEST_PG_URL=postgresql+psycopg2://postgres@127.0.0.1:55432/postgres`, `ONLYFANS_TEST_PG_EMPTY_TEMPLATE=1`; run from `service/tests` with `PYTHONPATH` = service + service/tests. Whole Plan C set ≈ 1 h on one CPU → run your area first; wide set on request (slop gate owns it). Dashboard: `npm run typecheck` (4 tsc configs) + node tests under `tests/components`. Failing test first.

## 9. Done, branches, stop-and-ask
Report: what/why, the failing test written first, suites run with pass counts, suites not run, assertions changed, migration/pins touched, open doubts. No generated files/.env/logs/evidence in git.
**Branch flow (every task):** work on your own branch `jaka/<topic>-<date>` cut from the current working branch (`handover/TEAM-BRIEFING.md` §2, it moves — the team keeps pushing). Commit + push there as often as you like. Before the PR: `git fetch`, **squash to one commit per topic**, **rebase onto the current working branch** (`git rebase origin/<working>`), re-run the area tests, `git push --force-with-lease origin jaka/<topic>-<date>` (your branch only; force on `claude/*`, `codex/*`, `main` is blocked by the hook). PR targets the working branch, never `main`.
Stop and ask when: touching safety/budget/ownership/stop/takeover logic; any migration or schema change; tempted to edit a failing test; anything beyond offline; two designs that affect money, messages or isolation. Open items: catalogue/terms, scope-transition, history/ownership designs exist as plans — ask before building; some old tests pin 0129 and differ between trial and commercial lines.
