# Run order for twin-onlyfans-backend — paste one prompt per session, in order

Plan: `docs/plan/CHECKLIST.md` (§A–G), `docs/plan/SECTIONS.md`. Rules: `handover/TEAM-BRIEFING.md`, `REVIEW.md`. Source of truth for all of `docs/plan/` and `handover/`: the twin-ai-kit repo (copied 2026-11-11 at fd5fde5; re-copy, don't edit here).

Facts for this repo:
- Start branch (the drop): `origin/claude/commercial-r0-r3-20261111`. Never base on anything else.
- Pre-handover ref: `659ea29` (claude/plan-c-deletion-proof-20261111 = what the live trial runs).
- Working branch: `jaka/kit-install-20261111`. Own branches: `jaka/<topic>-<date>` cut from it.
- Scope: backend `service/onlyfans_*`, `fans_api_adapter`, `conversation_engine`, `model_transport`, `db` (with care), `tests`.
- Push: our GitHub account is read-only on this repo and forking is off. Until that changes, "push" = `git push https://github.com/BlackPhoenixSlo/twinagency-local.git <branch>:backend/<branch>` (private backup) and the `Ship:` line says `PR none · merged no, read-only`.
- Offline only: no SSH, deploys, AWS, real model/provider calls (TEAM-BRIEFING §4).

## 1 · Absorb the drop (section 08)
```
Read handover/TEAM-BRIEFING.md, REVIEW.md, then run docs/plan/prompts/08-absorb-drop/prompt.md for THIS repo only.
Substitutions: pre-handover = 659ea29; handover/<date> = origin/claude/commercial-r0-r3-20261111; <date> = 20261111.
Human step 1 is done (drop already landed). Human step 2: create the tag locally (git tag pre-handover-20261111 659ea29) and the bundle in ~/backups/; do NOT push the tag (read-only).
Read-only Explore agents until step 5 is clean. Do not run lifecycle scripts. Regressions are findings, not fixes.
Step 6 baseline commands: python -m compileall -q service; the engine suites per TEAM-BRIEFING §8 (throwaway Postgres on 127.0.0.1:55432; your area first, not the 1 h wide set)
Step 7: lanes by directory, <=400 LOC or one feature; priority order = reply recovery, then one full sale. The team's Claude owns those fixes; our lanes review + test them.
Write docs/DROP-20261111.md on branch jaka/drop-20261111 (cut from jaka/kit-install-20261111) and W1 lanes in .scratch/rwl-W1/PROMPTS.md. Stop there and print the report.
```

## 2 · Takeover (section 00)
```
Run docs/plan/prompts/00-takeover/prompt.md for THIS repo only (one Explore agent). Read handover/TEAM-BRIEFING.md and handover/HANDOVER-ANSWERS-20261111.md first, then docs/DROP-20261111.md.
Start branch is origin/claude/commercial-r0-r3-20261111, not the repo default. Write docs/TAKEOVER-20261111.md and docs/RISKS.md on branch jaka/takeover-20261111. init.sh / features.json / claude-progress.txt: keep what exists, add only what is missing. features.json gets one entry per TEAM-BRIEFING §7 row that applies to this repo.
```

## 3 · Review rules + smoke (section 01)
```
Run docs/plan/prompts/01-review-groups/prompt.md for THIS repo. REVIEW.md already holds the briefing invariants: tighten each to this repo's file paths, delete rules that cannot apply here, keep root REVIEW.md and .claude/REVIEW.md identical. Then the smoke: branch jaka/review-smoke, 3-line harmless change, /rwl, confirm every lens ran or was reported unavailable, revert. Write docs/learn/01-review.md and start docs/review-ledger.csv. Stop for my approval of REVIEW.md.
```

## 4 · Cut window W1 (section 07)
```
Cut window W1 for this repo from docs/DROP-20261111.md (lanes by directory, not by merged PR): follow docs/plan/prompts/07-window-review/cut-window.md steps 3-6 with BASE = 659ea29, head = origin/claude/commercial-r0-r3-20261111. Create .scratch/rwl-W1/{COMMON,PROMPTS,STATE,HANDOFF}.md from the templates in docs/plan/prompts/07-window-review/. Lane order: reply recovery first, then one full sale, harness/REVIEW.md last. Read-only on code. Stop and wait for me to say "launch".
```

## 5 · After W1 launches
```
Build the rules->tests table: for each row of handover/TEAM-BRIEFING.md §7 that applies to this repo, write the failing test first (red), report it, do not fix engine code (the team's Claude owns it). One features.json entry per row. Branch jaka/rules-tests-20261111. Stop-and-ask list in TEAM-BRIEFING §9 applies.
```

Later sections (02 03 04 05 11, 06 after each): paste `docs/plan/prompts/<nn>-*/prompt.md` with "for THIS repo only".
