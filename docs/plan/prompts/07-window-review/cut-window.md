# Cut a window (paste to the orchestrator thread)

Repo <REPO>. Previous window ended at `<BASE_SHA>` (W<N-1>, PR #<n>). Create `.scratch/rwl-W<N>/` with COMMON.md, PROMPTS.md, STATE.md, HANDOFF.md from `prompts/07-window-review/*.template.md`. Read-only on code.

1. `git fetch`; head = `origin/<main>` sha. PR list: `gh pr list --state merged --base <main> --search "merged:>=<BASE_DATE>" --json number,title,files,body,mergedAt --limit 200`. Exclude PRs whose head matches `^rwl/W` (already reviewed; their fixes only) and open PRs (list them as "not in scope").
2. Cluster PRs by changed files into 8–12 lanes (shared ownership = same lane; `service/server.py`-style hubs go to the lane that changed them most; slop/harness + skills + REVIEW.md always the LAST lane, wave C). Name each lane `<slug>`.
3. For each lane write the PROMPTS.md block: `/rwl W<N>-<slug>. Read <abs path>/COMMON.md first and follow it.` + `PRs #…` + `Key: <files>` + `Focus:` = the invariant-driven risks for THOSE files (authz boundaries, false success, unbounded retries/calls, restart safety, money/sends, UI claims vs behaviour). Mark blockers "(blocker)". Optional `OWNER FEATURE` paragraph only if the human asked.
4. Seeds: read each PR body + CI; anything merged with "do not merge"/draft wording, red tests on main, unverified claims, medium-scale reds → STATE "Seeds" with the lane that owns it.
5. Waves: A = lanes with no shared hot files (max in flight <CAP>, start 5), B = the rest, C = harness last. Write the launch plan + the tick procedure into STATE.md.
6. Stop. Human reads COMMON.md + PROMPTS.md and says "launch".
