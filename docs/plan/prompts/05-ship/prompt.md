# 05 · Ship (PR → merge → gated deploy → watch)

Facts (measured 2026-11-10): twin-dashboard + twin-bot deploy on push to `main` (GH Action → ECR → SSM `twin-pull.sh`, auto-rollback on bad start; `deploy.yml` ignores only `*.md`/`*.txt`/`archive/**`). twin-onlyfans-backend has NO `main`; default `codex/onlyfans-of-b1`; ~40 branch-pinned workflows with AWS creds — pushing to those branch names deploys. Backend rollback is forward-fix only after schema 0098 (never restore the pre-0098 dump). Boxes reachable via Tailscale only.

Per task: branch (never `main`, never a workflow-pinned name — check `.github/workflows/*.yml` `branches:`), `/rwl` APPROVE, `slop/run --changed` GREEN (folder), `docs/learn/<slug>.md`, commit (trailer line), push `origin HEAD`, `gh pr create` (what/why, verdict, slop folder, screenshots, learn link, `found N · fixed N · open N` + "## Open findings"), link the PR, wait for checks. Human merges `main` until the gate is trusted (owner decision; revisit after W2).

Deploy = separate step, never from a review thread, never during a window (section 07): a `deploy-all` style skill per repo: leak gate on the exact tree (secrets, DB files), typecheck + import smoke on that tree, DB backup/checkpoint first (`twin-ops` `supabase-backup.py`; verify with `verify-restore.sh` monthly), then the repo's own path (merge to `main` for dashboard; `operations/*` + SSM for backend), verify live SHA + health, record in `docs/DEPLOYS.md` (date, sha, images, rollback image). Green gate deploys without asking; red gate stops.

After deploy: add `WATCH.md` lines (`- <id> | added <date> | expires +3d | <check> | <PR url>`) for every shipped behaviour so the 4 h sweep watches it; no fitting check → add one in the same PR. Tracker: move the ticket `pushed → live` via the team's flow (`tools/pushes.ts` logs pushes; operator closes).
Last line: `Ship: PR <url> · merged <sha|no + why> · deployed <yes <targets>|no + why|n/a>`.

## Branch flow (owner rule 2026-11-11)
**Branch flow (every task):** work on your own branch `jaka/<topic>-<date>` cut from the current working branch (`handover/TEAM-BRIEFING.md` §2, it moves — the team keeps pushing). Commit + push there as often as you like. Before the PR: `git fetch`, **squash to one commit per topic**, **rebase onto the current working branch** (`git rebase origin/<working>`), re-run the area tests, `git push --force-with-lease origin jaka/<topic>-<date>` (your branch only; force on `claude/*`, `codex/*`, `main` is blocked by the hook). PR targets the working branch, never `main`.

## Also do (from GAPS.md)
- Row 4: merge to `main` only through rulesets (section 10); the kit's `guard-main-push.sh` hook refuses pushes from a main checkout.
- Row 7: `docs/runbooks/rollback.md` — VPS keeps N image tags + switch script; dashboard = revert-PR fast path; DB never down-migrated (forward-fix only after schema 0098); post-deploy gate = check-light vs prod via monitor identity + error-rate for 15 min.
- Row 8: migration PRs need `supabase db diff` empty on staging; expand/contract across deploys.
- Row 21: actions pinned by SHA, `permissions: contents: read`, zizmor green.
- Row 22: fix PRs pass `scripts/ci/test-guard.sh` (assertions non-decreasing, no new skip/.only, `TEST-CHANGE:` line for deletions).
- Row 39: the human prod-ops path (migrate/rollback/restore) uses the `prod-ops` profile + GitHub Environment `production` reviewer; never a dev shell.
- Ship line also carries tokens spent (row 38).
