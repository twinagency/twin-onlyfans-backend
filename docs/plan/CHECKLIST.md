# End-to-end checklist

Tick in order. Each step: what you do · what AI does · how you verify. One section per day or two; the whole list is about a month.

## A. Prepare (before the team's push lands)
- [ ] Send the team `prompts/09-access-rotation/HANDOVER-ASK.md`. Confirm they will push to `handover/<date>`, not `main`.
- [ ] Machine: Docker up, `~/.slop/gate.py` present, `codex` logged in, 3 Claude accounts in T3 (`accounts.md`), DeepSeek provider visible in `orchestrator_capabilities`.
- [ ] `./install.sh --global` once. Then per repo on a branch: `git checkout -b kit/install && ../twin-ai-kit/install.sh .` (refuses on `main`).
- [ ] Open the install PR; in the same PR add `.claude/**`, `REVIEW.md`, `docs/**`, `slop/**`, `.scratch/**` to `deploy.yml` `paths-ignore` (dashboard) so kit files never trigger a prod deploy. Merge the team's existing `settings.json` hook with `settings.kit.json` by hand.
- [ ] Copy `CONNECT.env.example` names into each repo's `.env.example`; real values only in local `.env` (gitignored).
- [ ] Open repo in T3; `/rwl`, `/th`, `/freshen` autocomplete; `.claude/hooks/guard-main-push.sh` present and executable.

## B. Absorb the drop (section 08, the day it lands)
- [ ] You: `git tag pre-handover-<date> <prod sha>` + push tag; `git bundle create` to `~/backups/`.
- [ ] AI: run `prompts/08-absorb-drop/prompt.md` → read `docs/DROP-<date>.md`: instruction-surface diff verbatim, unicode clean, secrets result, deps added, baseline before/after, W1 lanes.
- [ ] Any `.claude/`, `AGENTS.md`, workflow, or lifecycle-script change in the drop: you read it line by line before any agent opens the repo again.
- [ ] Any secret found: row in `ACCESS-REGISTER.md` marked ROTATE (section C).

## C. Lock the doors (sections 09 + 10)
- [ ] AI: `docs/ACCESS-REGISTER.md` + `scripts/access-wizard.sh`. You: GitHub 2FA → AWS root MFA + billing alarm → Supabase org MFA → AI keys spend caps → `.env` escrow. Rotate only what gitleaks found.
- [ ] You: GitHub Team plan; rulesets on `main` + backend default (PR, 1 review, required checks, linear, no force-push, no admin bypass); `production` environment reviewer; Dependabot alerts.
- [ ] AI: CODEOWNERS, hooks wired, gitleaks pre-commit + CI, zizmor, SHA-pinned actions, `test-guard.sh`, `docs/AGENT-POLICY.md`, sandbox proposal (keys verified).
- [ ] Verify: push from a `main` checkout refused (hook message); planted fake key caught by gitleaks; `.scratch/agent-audit/<date>.jsonl` has rows; `trufflehog --only-verified` = 0 on all repos.

## D. Connect (names only, verify each)
- [ ] Supabase **local** `127.0.0.1:54321/54322` via `~/Projects/twinagency/dev.sh up`; prod refs denied in `settings.json` + team hook.
- [ ] Staging: Supabase branch or 2nd project, no prod data. ONE read-only `monitor` credential for sweeps, stored only in the T3 task env (written exception).
- [ ] BrowserBase: key per env; used only through the `operator` agent; slop fakes it at the wire.
- [ ] AWS: profile name only; everything deploy-side read-only until section 05.
- [ ] Hourly checks: read-only Supabase (monitor role) + AWS (read-only profile) credentials in the T3 task env only.

## E. Run sections (paste the prompt; read the report; run 06 learn after each)
- [ ] 00 takeover → `docs/TAKEOVER-<date>.md`, `docs/RISKS.md`, `init.sh` works from a cold shell.
- [ ] 01 review groups → `REVIEW.md` approved by you; smoke `/rwl` shows all 5 lenses ran or reported unavailable; `docs/review-ledger.csv` exists.
- [ ] 07 window W1 on the drop → `cut-window.md` → you type "launch" → `schedule_task` the `window-tick` → lanes end with `Ship:` lines → `close-window.md` (deploy only what you merge; WATCH lines; thermo pass; triage).
- [ ] 02 UI speed markers → 3 pages loaded, `curl localhost:3001/api/perf` shows p95 per route; `slop/baselines/perf-baseline.json` tracked; LHCI runs on a PR preview.
- [ ] 03 health sweeps → `schedule_task` from `schedules/sweep-4h.md` + `sweep-11h.md`; first reply ≤8 lines; one deliberately broken signal goes red; runbook per check exists.
- [ ] 04 slop → `slop/run --changed` green; `slop/run all` green once at medium=120; break-it-once red on the targeted check only; nightly scheduled from `schedules/nightly-slop.md`; `kill-switch` scenario green.
- [ ] 05 ship → every PR: `/rwl` green, slop green, you merge, deploy confirm, WATCH line, `Ship:` line with tokens; `docs/runbooks/rollback.md` exists and was read.
- [ ] 11 compliance → `docs/compliance/*` with owners + dates; scrub gate break-it-once; counsel booked.
- [ ] 06 learn → `docs/learn/<section>.md` per section; `CONTEXT.md`, `docs/adr/`, `DEFINITION-OF-DONE.md`, `RACI.md`, `ONBOARDING.md`; first `docs/cost/YYYY-MM.md`.

## F. Daily / weekly routine
- [ ] Daily 10 min: 4h/11h sweep lines (silence 2× = red); nightly slop `summary.md` + folder; open PRs → `/rwl <PR#>`; one 06 question.
- [ ] Red sweep → `prompts/03-deep-checks/triage.md`; red slop → `prompts/04-slop-daily/triage.md`; unexplained agent action → `.scratch/agent-audit/`.
- [ ] Per merged batch: next window W<N> (section 07).
- [ ] Weekly: `docs/RISKS.md` review; prod Security Advisor glance; WATCH lines expired; `docs/**` freshness flags.
- [ ] Monthly: restore drill; review-ledger precision per lens; cost ledger; reviewer canaries (quarterly); dependency/SBOM/licence report.

## G. Verify the kit itself
- [ ] `git -C twin-ai-kit status` clean; pushed; tag matches README version.
- [ ] New machine: `git clone` + `./install.sh --global` + per-repo `./install.sh` = same setup; `~/.claude/project-kit` resolves.
- [ ] `generic/FILL-IN.md` placeholders all have a value for a new project before using `generic/`.
