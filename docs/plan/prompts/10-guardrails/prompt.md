# 10 · Guardrails that are not prompts (server-side + OS-level + CI)

Goal: the things that protect prod even when an agent, a prompt injection, or a tired human gets it wrong. Pattern denies in `settings.json` match literal text and are **not a security boundary** (Claude Code docs). This section adds the boundaries.
GAPS.md rows: 4, 16, 17, 18, 19, 20, 21, 22.

Human (console; AI cannot and must not):
1. GitHub plan: private repos on Free have **no** branch protection. Confirm Team (or make the repos public — no). Then rulesets on `main` (dashboard, bot) and the backend default branch: require PR, 1 approving review, required status checks (typecheck, tests, gitleaks, zizmor; `slop --changed` when section 04 exists), linear history, block force-push and deletion, **no admin bypass**. Add a ruleset protecting the backend's workflow-pinned branch names from deletion/force-push.
2. GitHub Environment `production` with you as required reviewer on deploy jobs.
3. Dependabot alerts on (free for private).

AI does (branch `kit/guardrails`, PR, owner merges):
1. `.github/CODEOWNERS`: `*` → owner; `.claude/**`, `REVIEW.md`, `.github/**`, `supabase/migrations/**` → owner only.
2. Branch-aware push guard: install `.claude/hooks/guard-main-push.sh` (shipped in the kit) as a `PreToolUse` Bash hook — blocks `git push` when the working tree is on `main`/`master` or the refspec names them, regardless of command spelling. Test it: on a scratch clone, `git checkout main`, ask the agent to push → must be refused with the hook message.
3. Audit trail: `.claude/hooks/audit-log.sh` as `PostToolUse` (`matcher "*"`) → `.scratch/agent-audit/<date>.jsonl` (session, tool, input head, cwd). Gitignored; kept 90 d; section 03 triage reads it when something unexplained happened.
4. Sandbox: propose `settings.json` `sandbox` block — enabled, network allowlist (github.com, registry.npmjs.org, pypi.org, localhost; **never** `*.supabase.co`, OpenRouter/ElevenLabs/Deepgram/DeepSeek hosts), `.env*` unreadable, bypass-permissions mode disabled. **Verify every key against the current Claude Code docs** (`claude-code-guide` agent) before enabling; wrong keys are silently ignored.
5. Secrets tooling: `.pre-commit-config.yaml` with gitleaks; `.github/workflows/secrets.yml` gitleaks on PR + weekly `trufflehog --only-verified`; `.gitleaks.toml` allowlist for the fake local Supabase keys only.
6. CI hygiene: every action pinned by SHA; `permissions: contents: read` default; no `pull_request_target`; `zizmor` job; any workflow that runs an agent never reads issue/PR comment text. Audit the backend's ~40 branch-pinned workflows: list which carry AWS creds and which could be deleted (do not delete; report).
7. Test-weakening guard `scripts/ci/test-guard.sh` (runs on PRs labeled `fix` or `agent`): assertion count in `tests/**` non-decreasing; no new `skip`, `xfail`, `.only`, `@pytest.mark.skip`; any deleted test needs a `TEST-CHANGE:` line in the PR body. Red = the PR fails.
8. `docs/AGENT-POLICY.md` from `AGENT-POLICY.template.md`; link it from `CLAUDE.md`.
9. Budgets: every T3 scheduled prompt and every `claude -p` invocation carries `--max-budget-usd` / `--max-turns` (or the T3 equivalent line); abort = RED with reason, never silent.

Proof: push refused on main (paste the hook output); audit JSONL has a row for a test tool call; gitleaks catches a planted fake key in a scratch commit (break-it-once) and is clean on real history; test-guard fails a PR that adds `.only`; zizmor clean. `docs/learn/10-guardrails.md`. `Ship:` line.
