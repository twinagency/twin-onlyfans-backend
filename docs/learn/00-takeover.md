# 00 · Takeover
- What: one-page map of runs/connections/danger/dead/review surface (docs/TAKEOVER-20261111.md), RISKS.md, features.json (TEAM-BRIEFING §7), claude-progress.txt.
- Why: new team needs to know what can go live by accident before touching code.
- Verify by hand: `find service/sessions -type f | wc -l` (relay boot = live sends); `find service/tests -mindepth 2 -name 'test_*.py'` (never run).
- Breaks it: team pushes new branches (step2, integration) → map stale; re-fetch.
- Concept: blast-radius inventory; verify-first session start (init.sh).
