# Agent policy — <repo>

What an unattended agent (scheduled task, long run, subagent) may do without a human in the loop.

**May, alone**: read anything in the repo (not `.env*`); run typecheck/lint/unit tests/`slop/run --changed`; open or update a PR on a `kit/*`, `fix/*`, `feat/*` branch; comment on PRs; write under `docs/`, `.scratch/`, `slop/evidence/`; call Codex/DeepSeek lenses within the review budget.

**Never, alone**: merge to `main` or any deploying branch; deploy, restart, or SSH; touch prod Supabase or any `*.supabase.co`; rotate or create secrets; send a message to a fan, creator, or Telegram chat; spend above the task budget; edit `REVIEW.md`, `.claude/**`, `.github/**`, `AGENT-POLICY.md`; delete tests or weaken assertions without a `TEST-CHANGE:` line; loosen slop budgets or health thresholds.

**Two-key rule**: anything irreversible (migration with DROP/RENAME, data deletion, key rotation, DNS) needs a written OK from the owner in the PR *and* a second human or Fable consult recorded in the PR.

**Labels**: every agent-opened PR carries label `agent`; the PR body ends with the `Ship:` line and tokens spent.

**When blocked**: write the question into the report and stop that step; never guess on a prod-affecting decision.
