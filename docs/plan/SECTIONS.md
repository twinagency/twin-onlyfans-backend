# Sections — one paragraph each (run order: 08 09 10 00 01 07 02 03 04 05 11; 06 after each)

## 08 Absorb the drop
The team's ~500 changes land on `handover/<date>`, never `main`. You tag and bundle the prod sha first. AI then diffs the *instruction surface* first (`.claude/`, `AGENTS.md`, workflows, lifecycle scripts) because a drop can carry text that agents will obey; lints hidden unicode; runs gitleaks + trufflehog on the history; baselines typecheck/lint/tests before and after; cuts the diff into ≤400-LOC lanes that become window W1; adds characterization tests on every touched endpoint. Nothing merges until W1 closes.

## 09 Access register
The team stays, so this is a register, not a purge: every credential is a row in `docs/ACCESS-REGISTER.md` (who holds it, where it lives, how to rotate). Agents are denied aws/ssh/prod, so AI writes the register + wizard and you run the human steps: GitHub 2FA, AWS root MFA + billing alarm, Supabase org MFA, spend caps on AI APIs, `.env` escrow with two holders. Rotate only keys that gitleaks/trufflehog show as leaked.

## 10 Guardrails
Pattern denies in `settings.json` match text and are not a security boundary. This section adds the real ones: GitHub rulesets on `main` (needs Team plan for private repos), CODEOWNERS, `production` environment reviewer, the kit's branch-aware `guard-main-push.sh` hook, a `PostToolUse` audit log, Claude Code sandbox (verify keys first), gitleaks pre-commit + CI, zizmor and SHA-pinned actions, a test-weakening guard, budgets on every unattended run, and `docs/AGENT-POLICY.md` saying what agents may do alone.

## 00 Takeover
AI reads both repos read-only, maps layout, deploy paths, env names, branches, and writes `docs/TAKEOVER-<date>.md`: what runs where, what is dangerous (prod DBs, deploy-on-push, 40 branch-pinned workflows), what is dead, top five risks → seeded into `docs/RISKS.md`. Creates the session artifacts (`init.sh`, `features.json`, `claude-progress.txt`) every later session starts from.

## 01 Review groups
Every repo gets a root `REVIEW.md` (mirrored to `.claude/REVIEW.md`) with this repo's invariants and failure modes. `/rwl` then runs structure + correctness reviewers (Opus, read-only, parallel), ONE fixer, Codex Sol r1/final, Codex Astra devil's advocate, DeepSeek Flash on a scrubbed code-only diff — all tagged, merged into one stably numbered findings file, re-reviewed until APPROVE. A review ledger tracks which lens finds real bugs; planted canaries measure recall; migration and kill-switch rules live here.

## 07 Window review (W1, W2, …)
A window is the set of merged PRs since the last window. `cut-window.md` clusters them into ~10 lanes by file area, writes `.scratch/rwl-W<N>/{COMMON,PROMPTS,STATE,HANDOFF}.md`, and stops for your "launch". Each lane is a top-level T3 worktree thread running `/rwl`; a 20-minute `window-tick` task reads thread tails, marks done on the `Ship:` line, nudges stalls, flags limits, launches the next wave. `close-window.md` deploys, adds WATCH lines, runs the thermo pass and the open-findings triage (`tools/open_findings.py`), and sets the next base. This is the W1→W28 process from the reference repo.

## 02 UI speed markers
`performance.mark`/web-vitals in the dashboard's critical pages posted to `/api/perf`; backend stores p50/p95 per route per day in `perf_samples`. Baseline is tracked in `slop/baselines/`, updated only from green main; a red needs n ≥ 30 and a 7-day rolling baseline because operator traffic is tiny. Lighthouse CI on the PR preview is the stable gate; RUM is prod truth.

## 03 Health sweeps (4h / 11h) + ops
Two local T3 scheduled tasks. 4h = stack A cheap signals; 11h = stack B deep (real-path smoke on local, migration drift, RLS lints, dependency audit, docs freshness). ONE read-only collector, verdict decided in code (alert > unknown > watch > ok), declarative decaying WATCH lines, ≤8-line reply, dead-man rule (silence = red). Around it: runbooks per check, incident template, SLOs with burn-rate alerts, monthly restore drill, Renovate, one read-only monitor identity as the written exception to never-prod.

## 04 Daily full slop
ONE `slop/` per repo: seed by SQL, simulated users on real endpoints, fakes only at wire boundaries with last-verified dates, invariants (nothing dropped, order kept, no 5xx, p95 vs last green, deleted fan → 0 rows). `slop/run --changed` per task; 02:00 nightly `all` through `~/.slop/gate.py`, medium = 120 users, `status.json` + `done` contract, ≤2 fixes/night, never loosen budgets. Kill-switch scenario proves `OUTBOUND_PAUSED` stops sends in 5 s. Screenshots only from synthetic data.

## 05 Ship
Branch → PR → `/rwl` green → slop `--changed` green → you merge (rulesets + hook make it the only path) → deploy confirm via check-light + 15-min error rate → WATCH line → tracker `pushed → live` → `Ship: PR · merged · deployed` with tokens spent. Rollback runbooks for VPS, dashboard, and DB (forward-fix only).

## 11 Compliance
Written, owned, dated: OnlyFans ToS risk (AI DMs banned, impersonation = evasion), EU AI Act Art. 50 disclosure, GDPR Art. 9 DPIA + processor list + retention, DeepSeek scrub gate, PII-free evidence. AI drafts the stubs and the counsel questions; you book counsel and decide. Not legal advice.

## 06 Learn
After each section AI explains in ≤10 lines what changed, why, how to verify by hand, what would break it; you ask until you could redo it. Plus the team-facing memory: `CONTEXT.md` glossary, ADRs, definition of done, RACI, onboarding, and a monthly cost ledger (tokens + invoices) so model/effort defaults come from numbers.
