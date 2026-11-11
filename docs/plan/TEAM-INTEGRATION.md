# Team integration — what already exists, plug in instead of duplicating (measured 2026-11-10, shallow scan)

| kit piece | team already has | do |
|---|---|---|
| tickets / todo | tracker in Supabase (migrations 110–118), `/tracker` page, lifecycle `open → fixed → pushed → live → closed`, only operator closes; CLI `tools/tracker.ts`, `tools/pushes.ts`, `tools/tick.ts`, `tools/twin-release.ts` (twin-tgaiclone) | kit tasks become tracker rows; `Ship:` lines feed `pushed → live` |
| pipeline | `pipeline/HOW-TO-USE.txt`: report → research (`claude -p` read-only) → build (scoped) → verify → deploy (`pipeline/deploy.ts --dry-run|--go`) → postverify; gates in `pipeline/lib/preflight.ts` (dirty tree, destructive migration, one deployer); steps 2–3 have no entrypoints yet | our `/rwl` + implementer = their steps 2–3; keep their gates |
| review rules | `reports/2026-10-31-of-stability-plan/AGENT-RULES.md`, `reviews/`, `tools/adversary.sh` (codex read-only); no REVIEW.md anywhere | REVIEW.md points at AGENT-RULES + CLAUDE "Verification"; adversary.sh = extra lens |
| permissions | `twin-tgaiclone/.claude/settings.json` has a PreToolUse hook denying `cnnsllgzskqkvdovyiiv` + allowlist; `pipeline/lib/session.ts` per-step tool allowlists | install.sh never overwrites; merge `settings.kit.json` by hand |
| proof harnesses | `.local-dev/prove.mjs` (`dev.sh prove`: login, DB pages, one fan message, screenshots, no 5xx/egress; evidence `.local-dev/evidence/<UTC>-bot<sha>-dash<sha>/`), `bot/src/dev/check-*.ts`, `*/prove-checks.sh` (plants 20 bugs), backend capacity workflow | slop/ absorbs load tests (their ticket 04); keep prove.mjs as the smoke |
| scheduling | ONE crontab line: `tools/tick.ts` every 5 min; bot schedules `tracker_bots.every_minutes`; boxes: `watchdog.sh` every minute (never restarts), nightly backup 17:00 UTC (`supabase-backup.py`, 30 copies, `verify-restore.sh` drill) | sweeps stay in T3 on the Mac; in-repo schedules only via tick/tracker_bots |
| ops | `twin-ops`: `deploy.sh --dry-run`, `rollback.sh` (image only), `twin-pull.sh` (auto-rollback), `check-secret-drift.sh`, `check-uncommitted-migrations.sh`, `ship-logs.py`; Tailscale only | section 05 wraps these; never a second deploy path |
| reader bots | snitch, triage, ledger, repro, warden, blackbox, dedupe (write only `tracker_*` rows) | sweep findings could file tracker rows later (not now, YAGNI) |
| status format | `LOW LEVEL` / `HIGH LEVEL`, ~20-word replies, operator cannot open files | all tracker-facing reports |
| docs | twin-dashboard keeps only CLAUDE.md + README.md in repo; truth hierarchy code > DB > docs; `archive/` = history | `docs/learn/` allowed in backend + process repo; for dashboard keep learn notes in twin-tgaiclone |

Handover facts to carry into prompt 00: live versions table in backend `ACTIVATION-RECORD-20261028.md` §1; 7 open items (§8); rollback forward-fix only; backend has 194 remote branches and no `main`; dashboard local is behind; `twin-tgaiclone/README.md` still links `ronan-com/*`.
