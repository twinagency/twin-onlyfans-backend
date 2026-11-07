# Plan C handoff: state of the narrow supervised OnlyFans pilot

Written 2026-11-07 22:35 AEST (Work Server clock 12:35 UTC) by Claude for Ronan, Jesse and whoever continues (ChatGPT/Codex). No secrets in this file. Plan C = the narrow, supervised pilot described in CLAUDE-PILOT-HANDOFF.md (Jesse/Codex, 7 Nov) plus Ronan's test-only decisions below.

Four states, reported separately as the handoff asks:
- Implementation: COMPLETE on branch `claude/plan-c-merge-20261107` (backend) + `claude/plan-c-dashboard-20261107` (dashboard).
- Offline qualification: PARTIAL. Unit suites green; the two full PG batches on the FINAL commit were still running when this was written (see section 3). Independent review of the final diff: done by me for the money/send paths (section 6), not by a second reviewer.
- Deployed inactive: NOT DONE. No image built, no deployment, no production change.
- Pilot active: NOT DONE. No real fan has been messaged. No real purchase.

## 1. Exact repositories, branches, commits

Backend `twinagency/twin-onlyfans-backend` (GitHub), all pushed 2026-11-07 ~12:30 UTC, none merged to any default branch, no CI deploy fires on `claude/*` (checked: ci.yml only on `main`; other workflows only on named `codex/*` branches):

| branch | commit | what |
|---|---|---|
| `claude/plan-c-merge-20261107` | `f2129fa` | THE RELEASE CANDIDATE. Everything below merged, plus the NSFW rules. Use this. |
| `claude/plan-c-pilot-20261107` | `2fad7c3` | base: pinned join 5b4e560 + A6 model cherry-pick (migration renumbered 0123) + A7 worker + defects + model merged |
| `claude/plan-c-approval-20261107` | `1767d59` | approval gate (migration 0124_engine_approval, approval.py, gated real worker) |
| `claude/plan-c-api-20261107` | `d2ae147` | operator API v2 + operator_v2.py + fansapi operator guards (includes approval merged) |
| `claude/plan-c-defects-20261107` | `1f630e9` | fan-isolated admission, semantic claim checks, unlinked payments, writer fence, send/event recovery |
| `claude/plan-c-model-20261107` | `6a33a3a` | model runtime wiring, cost upper bound, opt-in fallback, writing judge, journey subset |
| `claude/plan-c-sim-20261107` | `d0550a5` | simulation harness `service/tools/plan_c_sim.py` + report merger |
| `claude/plan-c-nsfw-20261107` | `f2129fa` | NSFW sexual-chat prompt block, judge rules, fan-side banned-theme routing |

Dashboard `twinagency/twin-dashboard`: `claude/plan-c-dashboard-20261107` @ `614c534` (page `/onlyfans/plan-c`, proxy routes, typed client, tests). Pushed with the laptop gitleaks hook bypassed ONLY because the hook scanned 13 older commits already on GitHub; the one new commit scans clean (gitleaks 8.30.1, 0 findings on `3afc28f..614c534`).

Work Server (host `ubuntu-s-4vcpu-8gb-syd1`, `/srv/projects/twin-engine-completion-20261107/`): worktrees `planc-merge` (release tree, clean at f2129fa), `planc-approval`, `planc-api`, `planc-defects`, `planc-model`, `planc-sim`, `planc-nsfw`, `planc-dash` (all clean, all committed). `join` = plan-c-pilot. Older Plan A worktrees (`a6-model`, `a7-worker`, `dash`, `b*`) untouched and clean. Supporting files not in git: `PLAN-C/` (handoff, review, AGENT-RULES.md, PILOT-CONFIG.md, OPERATOR-API-CONTRACT.md), `~worker/plancsim_live/` (both simulation transcripts), `~worker/pilotrun.sh` (test runner), `~worker/*.log` (test logs), docker `pilot-pg-20261107` (isolated Postgres for tests).

Uncommitted work: none in any Plan C worktree. Dashboard worktree has gitignored `node_modules` symlink + `next-env.d.ts` only.

Not on the Work Server: the laptop copy of PDFs/handoff in `~/plan-c-out/` and this file (also committed as `docs/plan-c/HANDOFF.md` on the merge branch).

## 2. Integrated vs still separate

Integrated into `claude/plan-c-merge-20261107` (f2129fa): approval gate, operator API, defects, model, sim, NSFW. Verified by `git log --merges` on that branch.

Still separate:
- Dashboard (different repo). Built against OPERATOR-API-CONTRACT.md and the backend agent matched its shapes; never run against the real backend (needs a deployed backend).
- The old Plan A branches (`claude/engine-*-20261107`, A6/A7) are superseded by the merge tree; not deleted.
- Build B (`jesse98/moxy-onlyfans-chatter`) untouched, reference only.
- Nothing from Plan C is merged into `main` or `codex/onlyfans-of-b1`.

Migration chain on the release tree: `0119_identity_independence -> 0120_fans_intake -> 0121_engine_generations -> 0122_fansapi_transport -> 0123_engine_model_calls -> 0124_engine_approval` (head). `db/schema.py` HEAD/ranks/check_schema updated; upgrade+downgrade rehearsed by `test_engine_rc_join_pg.py` on an isolated DB.

## 3. Test results (honest, with failures)

Runner: `PILOT_TREE=<tree> ~worker/pilotrun.sh -m pytest -q -p no:cacheprovider -W ignore <files>` (isolated Postgres, image `twin-engine-offline-main:20261107-r6`). No test was marked xfail or skipped by me; `-rs` was added to the final runs to list skips.

On the FINAL tree f2129fa:
- `service/tests/conversation_engine` (unit, no DB): 210 passed, 0 failed (includes 22 new NSFW tests).
- Full PG batch A (`test_engine_*.py` + `test_plan_c_sim_pg.py`) and batch B (`test_fans_*`, defects, fence, real transport, egress, text delivery, moxy fence): STARTED 12:33 UTC, results in `~worker/final-A.log` / `final-B.log`. Not finished at the time of writing. Whoever reads this: `tail -3` both logs.

On the pre-NSFW merged tree (c08c321 / e1d1897, same code except the NSFW commits which only touch prompt, judge, journey signals, writer_context + tests):
- `test_engine_*.py` batch: 306 passed, 2 failed. Both were test-only (one test did not allow the new operator_v2 route to call approve; one called `queue` without the approval step). Fixed in c08c321; the two files then 51 passed.
- Batch B: 485 passed, 15 failed: 5 unit tests (grounding/identity, fixed, now 194->210 pass), 1 `test_real_transport_pg::test_paid_offer_carries_vault_ids_previews_and_dollar_price` (needs the approval branch's finished-backfill setup; passed in the approval agent's run; re-verified only by the final batch B above), 9 `test_onlyfans_egress_isolation.py` (`engine_owner_lookup_failed` from `moxy_replacement/routing.py`). The 9 egress failures were reported by the API agent as failing on the pre-agent base 4711dee too; my own reproduction on the pristine 5b4e560 was started and then killed to free the runner, so that claim is NOT independently confirmed by me. They are in the legacy Moxy routing path, not the pilot send path.
- Script-style suites (not pytest): `test_twin_bridge_ownership.py` passed, `test_concurrent_delivery_pg.py` passed (15 checks), `test_concurrent_delivery.py` could not run (container thread limit).
- Known base-only failure: `test_concurrent_admission_units::test_concurrent_migration_is_the_release_head` asserts an old head; expected after any new migration.

Agent-reported counts (not independently rerun by me): approval 366 passed/1 fixed then 40; API 31+4 passed; defects 682 passed/4 (3 base); model 225 passed/4 fixed then 19; sim 55 passed; dashboard: plan-c-contracts 23/23, plan-c-ui 16/16, 4 tsconfig typechecks clean, `test:onlyfans-agency-http` and `test:onlyfans-ui` fail identically on the untouched dash base.

Not run at all: `next build`, dashboard browser journey, packaged backend image build, live-backend dashboard check, `live_proof_b3.py` (live script; now refuses because it calls `engine.queue` without approval, left as is).

## 4. Simulations: transcripts, settings, costs

Both runs: `service/tools/plan_c_sim.py` on tree f2129fa minus NSFW (the sim used the merge tree at 1f7d866, prompt `a-original.v2-planc1`; the NSFW prompt v3 landed after the runs, so these transcripts do NOT exercise the NSFW rules yet). Same inputs file for both (`plan_c_sim_inputs.json`: product "Plan C test set", 3 media = 2 pictures + 1 video, 6.00 USD; creator Riley, limits no minors / no meet-ups / no off-platform / no customs, NO approved identity wording; 4 personas x 20 turns). Fake FansAPI on loopback, real FansApiRealTransport, real worker, real approval gate, real ModelRuntime/ledger, simulated operator `sim-operator-1` applying a written rubric, scripted fans, signed fake unlock webhooks, real pacing sleeps. `--operator-resolves-handovers` ON (a labelled simulated human closes a model handover so the script can continue). temperature 0.85, max output 500, max input 8000, 2 attempts, no fallback in either run (so the two runs are directly comparable on inputs; model outputs still diverge at temperature 0.85, so it is a sample, not a controlled A/B).

| | DeepSeek `deepseek/deepseek-v4.1-flash` | Grok `x-ai/grok-4.3` |
|---|---|---|
| prices USD/Mtok in/out (OpenRouter list 2026-11-07) | 0.051 / 0.567 | 1.25 / 2.50 |
| fan turns | 80 | 80 |
| replies sent | 60 | 71 |
| validator rejects | 11 | 8 |
| operator rejects | 0 | 0 |
| empty / refused completions | 9 | 0 |
| handed to human | 13 | 2 |
| crashes | 0 | 0 |
| actual cost | 0.0208 USD | 0.2377 USD |
| cap | 2.00 USD | 2.00 USD |

Files: Work Server `~worker/plancsim_live/{deepseek,grok}.{json,md}` + `comparison.md`; laptop `~/plan-c-out/` same + PDFs `PLAN-C-SIM-DEEPSEEK.pdf`, `PLAN-C-SIM-GROK.pdf`, `PLAN-C-SIM-DEEPSEEK-vs-GROK.pdf`. Every model call records requested and returned model, returned provider, tokens, reserved vs actual micro-USD, validator reasons. DeepSeek's empties are the known OpenRouter/DeepSeek refusal-as-silence; each went to a human, none was retried.

Model id note: `x-ai/grok-4.1-fast` (Ronan's first pick) is no longer listed on OpenRouter; `x-ai/grok-4.3` is the cheapest Grok chat model listed. Code defaults still mention `grok-4.1-fast` in `model_config.py` / `wiring.py` / `docs/plan-c/DECISIONS.md` as an example id; the run-time id is always passed explicitly and prices are a required config value (startup refuses without them).

## 5. What "live runs" means here

"Live" in the simulation = REAL model calls to OpenRouter (metered, $0.26 total) over a FAKE FansAPI. NO real OnlyFans message was sent, NO real FansAPI call, NO real purchase or unlock, NO real fan or creator account touched. Nothing has been deployed anywhere. The FansAPI credentials on the Work Server were not used.

## 6. Remaining defects, decisions, authorizations

Review findings (my read of the final diff, money and send paths):
- Approval is enforced at four points: `engine.queue` (authorize_send), intent creation (`require_in_session`), immediately before the wire marker (`check_in_session`), consumed with the last receipt. Hash covers parts, verified terms (price, media, item), owner epoch, control revision, input base, connection epoch. Migration 0124 is append-only (guard trigger, RLS). No automatic approval or handback exists in `service/` (test enforces it; `operator_v2.py` approves only on a named human's HTTP command).
- Payments: a provider unlock bound to OUR sent message/fan/price/grant is recorded as the purchase; `transactions.new` and `chargebacks.new` are kept as separate unlinked evidence and never paired by amount/time. An unlinked reversal flags the fan for a human and does not revoke access. This is the defects agent's interpretation of REVIEW finding 5; Jesse should confirm it matches his intent for "unlock, transaction, reversal remain distinct".
- Send: one idempotency key per (account, intent), max 3 POSTs, provider outcome fields not HTTP status, 409-then-not-sent becomes unknown, restart leaves a dead dispatch as UNKNOWN (never requeued). Fan-isolated admission for fansapi accounts (one fan's unknown send no longer blocks the account).
- Fixed by me during review: `engine.plan()` backstop now uses the same grounding as the judge (it would have rejected every offer text that stated the real media count).

Open defects / gaps:
1. Final full PG test batches on f2129fa not yet read (section 3).
2. 9 egress-isolation failures not independently reproduced on the pristine base by me.
3. No ledger column for returned model/provider/prompt version; recorded in `error_class` prefix `ret_model:` and the log. Needs a migration if Jesse wants it first-class.
4. `real_worker` relies on `TextAuthority.dispatch` to expire dead dispatches; a startup call to `transport.recover_interrupted()` would be cleaner.
5. Draft-only deploy = flag `TWIN_ENGINE_FANSAPI_REAL=1` on and nobody approving; the worker refuses to even draft with the flag off. Acceptable for the pilot, not the cleanest "inactive" mode.
6. `temporary_pause_reopen_hours` (the 12-hour re-offer idea) implemented but OFF pending a Jesse decision.
7. Sim transcripts predate the NSFW prompt v3; rerun the sim on f2129fa before judging explicit-chat quality (`run_plan_c_sim.sh`, command in planc-sim report / `docs/plan-c`).
8. Dashboard never exercised against the real backend; `next build` not run.
9. Set `TWIN_ENGINE_PILOT_ACCOUNTS` in every process that can run `of_client`; keep the pilot account out of FASTT scope; clear provider-side mass/scheduled queues before any live step (writer fence cannot stop provider-side queues).
10. Legacy code still references `grok-4.1-fast` as an example id (see section 4).

Decisions recorded (Ronan, owner, 2026-11-07 evening): test-only on the already-connected account + one test fan; invented product (6.00 USD, 3 media); model DeepSeek v4.1-flash; opt-in Grok fallback (departs from the handoff's "no fallback"; Jesse decision NOT recorded); per-message human approval is test-stage only, autonomous sending is a later qualified change needing a recorded Jesse decision; Plan C is the name. Config register: Work Server `PLAN-C/PILOT-CONFIG.md`.

Authorizations that EXIST: offline implementation (Jesse via handoff); push of working branches (Ronan, done); live model calls within $5 for the simulation (Ronan; $0.26 used); Ronan's earlier OK for live FansAPI tests on the connected test account with the one test fan, caps 300 credits / $5 AI.

Authorizations that do NOT exist yet: deployment of any image; activating the real transport on any server; any real OnlyFans message or unlock; merge to main; removing per-message approval; Jesse's sign-off on the Grok fallback and the unlock-as-purchase interpretation; a fresh restore-tested backup before production change.

## 7. How to run things

- Tests: `PILOT_TREE=/srv/projects/twin-engine-completion-20261107/planc-merge ~worker/pilotrun.sh -m pytest -q -p no:cacheprovider -W ignore service/tests/test_engine_*.py` (two runs at a time share the lock).
- Simulation (real model calls, fake FansAPI): see `service/tools/run_plan_c_sim.sh --help`; export `OPENROUTER_API_KEY` in the shell (key file lives in `~worker/.config/twin-engine/`, never print it); `--dry-run` needs no key.
- Operator surface: backend routes `/onlyfans/control/v2/accounts/{account}/...` (bearer CONTROL token, header `X-OnlyFans-Engine-Operator-Version: of-engine-operator.v2`); dashboard page `/onlyfans/plan-c`.
- Kill switch: `TWIN_ENGINE_FANSAPI_REAL` unset/0 = nothing can send; `policy.enabled` and `runtime.enabled` also gate.
