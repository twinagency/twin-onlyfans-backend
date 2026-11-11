# 03 · Health sweeps: 4 h (stack A) and 11 h (stack B), run locally on T3

Measured precedent: infloww runs `AE proxy + prod health check (4 h)` and `Public … (11 h)` as T3 scheduled tasks on the owner's Mac, plus the 02:00 nightly slop. Same design here. Nothing is added to the repos' cron (team rule: `tools/tick.ts` is the only cron); the sweeps are the owner's read-only observers.

Build (implementer, branch `kit/health-sweep`, per repo that has a prod):
- `scripts/health/collect.sh` — ONE remote command (ssh via Tailscale or AWS SSM `caption-ssm-exec`-style helper), read-only: `docker ps`/`inspect` health, `mode=ro` SQL (10 s/query, 20 s total) for the signals below, one log pass since the previous run's `run_at`, env reads. Writes `docs/health/runs/<UTC stamp>.json` + a ≤40-line `.md` digest. Exit 10 = unreachable. Never prunes runs. Scrubs `scheme://user:pass@` and IPs.
- `scripts/health/summarize.py` — verdict fixed in code (`decide`): precedence alert > unknown > watch > ok; `verdict_reasons` lists all. Alert = new impact in the interval (failed sends/charges, paused ingest, stuck runs, container unhealthy, disk >85%, 5xx rate, OF/session/API 401 storms per POLICY); unknown = data missing/timed out (never ok). Prints the digest.
- `scripts/health/watch.py` — `WATCH_CHECKS`: each check declared once (function, SQL keys, log filters, env). `WATCH.md` lines `- <id> | added D | expires +3d | <check> | <PR url>`; `plan()` turns unexpired lines into what collect.sh gathers; expired/broken → `drop` lines the sweep deletes.
- `docs/health/README.md` — signals table (check · source · watch · alert), **Runbook** (one line per signal: what to look at, never restart from the sweep), file layout. Only README is committed; `runs/ IDEAS.md INCIDENTS.md WATCH.md` gitignored (the sweep writes there and must not dirty the tree).
- Tests: `pytest scripts/health` with fixture digests; one deliberately broken signal → alert; missing data → unknown.
- Starting signals (dashboard): clones heartbeat age, bot jobs stuck >2 h (`tracker_bot_jobs`), fan message→reply p95, 5xx/h, Supabase connection errors, watchdog alerts count. Backend: relay health, OF session dead_new, queue depth, send failures, 401/429 per hour vs baseline, worker restarts, disk.

Schedule (AI, after human says go): `schedule_task` with `schedules/sweep-4h.md` filled for stack A (interval 4 h) and `schedules/sweep-11h.md` for stack B (interval 11 h); dedicated detached worktree per sweep (`~/<repo>-health`); record ids in `docs/SCHEDULES.md`.

Proof: run collector by hand twice (ok); break one signal source (wrong container name) → `unknown`, not ok; plant one alert condition on local stack → `alert`; show WATCH expiry drop. `/rwl`. `docs/learn/03-sweeps.md`. `Ship:` line.

## Also do (from GAPS.md)
- Row 5: the ONE read-only `monitor` credential (RO pg role + health token) is a written exception to never-prod; it lives only in the T3 task env, never in a repo. Staging = Supabase branch or 2nd project, no prod data by default.
- Row 6: dead-man rule — no sweep reply for 2 intervals = RED; each run writes a `done` marker.
- Row 9: 11h sweep runs splinter RLS lints on local post-migration DB.
- Row 28: three SLIs (relay delivery success, dashboard availability, OF session validity) with burn-rate alerts; SEV1 → phone (Healthchecks.io / Better Stack free tier); quiet hours written down.
- Row 29: `docs/runbooks/<check>.md` per check (symptom, first 3 commands, rollback link, who to tell); `docs/incidents/TEMPLATE.md` blameless, 5 whys, actions into `docs/RISKS.md`.
- Row 30: monthly restore drill — download Supabase backup → restore local → `dev.sh prove`; RTO/RPO in `docs/runbooks/restore.md`; VPS rebuild-from-repo tested once.
- Row 31: `renovate.json` (automerge patch after CI, weekly, grouped); `syft` SBOM per release; license check fails on AGPL/unknown.
- Row 42: 11h sweep flags `docs/**` with frontmatter `reviewed:` older than 30 d; `lychee` link check.
- Row 18/19: triage reads `.scratch/agent-audit/*.jsonl`; every scheduled prompt carries a budget line; abort = RED.
