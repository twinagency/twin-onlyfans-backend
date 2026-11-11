# 00 · Takeover (read-only)

You are the top-level session (Opus). Read `.claude/orchestration.md` and `REVIEW.md`. Spawn read-only `Explore` agents (one per repo) and report; you write only `docs/TAKEOVER-<YYYY-MM-DD>.md`. No code edits, no git writes except that one file on a branch `kit/takeover`.

Repos: `~/Projects/twinagency/twin-dashboard` (main = prod deploy) and `~/Projects/twinagency/twin-onlyfans-backend` (default `codex/onlyfans-of-b1`). First: `git fetch --all` and report whether the team's large push has landed (date of newest commit on origin, count of commits behind). If local has uncommitted changes, list paths only, touch nothing.

Produce `docs/TAKEOVER-<date>.md`, ≤1 page, sections:
1. **Runs where** — services, ports, Dockerfiles, deploy scripts, which branch deploys what.
2. **Connections** — every external system (Supabase projects, BrowserBase, AWS, Telegram, OnlyFans, the bus, AI providers): env var names (never values), who calls it, is it faked in tests.
3. **Dangerous** — anything that touches prod on push/run; mark with ⚠.
4. **Dead** — unused dirs/scripts/docs (candidates for `/freshen`, don't delete).
5. **Review surface** — where logic concentrates (files >1000 lines, god modules), test coverage shape, existing CI.
6. **Top 5 risks** for a new team taking over, each with a concrete first check.
7. **Open questions** for the human (max 5, one line each).

Label every claim measured / inferred / guess. Finish with: files read count, agents spawned, tokens if known.

## Read first (2026-11-11)
`handover/TEAM-BRIEFING.md` and `handover/HANDOVER-ANSWERS-20261111.md` (goal, roadmap steps 2→3→1, roles, code map, known bugs, env inventory, credential places). Then the code. Open items in the answers (OWNER TO FILL, U) → `docs/RISKS.md`.

## Also do (from GAPS.md)
- Row 37: seed `docs/RISKS.md` (id, risk, L/I, owner, mitigation, trigger, review date) from the top-5 risks; reviewed weekly in CHECKLIST §D.
- Row 23: create `claude-progress.txt` + `features.json` + `init.sh` (verify-first smoke) for this repo; every later session starts with `init.sh`.
- Runs AFTER prompt 08 (absorb-drop) when a team drop is pending.
