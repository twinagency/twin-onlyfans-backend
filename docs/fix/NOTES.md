# Fix-orchestrator decision log (backend)
Started 2026-11-12. Team head = origin/claude/commercial-r0-r3-20261111 a253c84c (no newer claude/commercial-* contains it, m).
- D1 step 0: sweep ~50 lane test files (lane-own commits, `git log b --not <team refs>`) against a253c84c; worker Opus high; PG port 55640. Results → .scratch/rwl-programme/FIX-QUEUE.md + docs/fix/FIX-QUEUE.md.
- D2 no known-red list exists in repo (grep m) → "promote" = add lane red test (now green) into service/tests in the fix PR.
- 18:5x FYI review-orch: W5+W6 triage 57a90538 (G14 ext, G15 money asks, G16 send-path fail-open ARMED engine.py:464-509 / sell_lane.py:467, G5/G8/G10+11 ext; W6HR-1 LIVE on worker = PR #83 in target). Red tests W6AA-1/-2, W6AD-5, W5NR-5 already in sweep list. Sending OFF 18:22 (m, W6 LV). No review lanes while fix mode on. No action change.
- D3 sweep (m): 231 cases, GREEN 4 (2 team fix #83, 2 W3DR-01 n/a), RA 140, RD 44, guard 43; 120 findings → 0 fully fixed-by-team, 94 queue, 25 drift-unknown. a2f1dda6 NOT ancestor of head (m). Order F1–F12 in FIX-QUEUE. W3RCB-01 partial → no OPEN-FINDINGS 'fixed-by-team' mark (not fully fixed).
- F1 PR #84 4a3c405c (+130/-6). rwl r1 (reduced: 1 Opus reviewer both lenses + DeepSeek Flash; no Codex/thermo — 28 product LOC): Opus 0 blk/2 maj, Flash 1 blk (from_curl relay path)/2 maj (premerge OK-on-skip). Merged → docs/fix/F1-review-r1.md; fixer = same F1 implementer (resumed).
- F1 r2: Opus delta review 1-3,5-8 CLOSED, 4 PARTIAL; N1 maj premerge BASE-RED exit 0; N2 minor. Process slip: SendMessage-resumed fixer was still running when nudge said idle; I dispatched a 2nd fixer → collision at 19:48 (2nd stood down, deleted 1 file). Lesson: before a new writer, check worktree mtimes/pgrep for the old one.

## F1 final Flash (deepseek-flash, full diff a253c84c..c0d960f4)
- Major, accepted+fixed: server.py bootstrap route let OwnerMismatch become 500 → now 409 owner_mismatch; new test test_bootstrap_route_maps_owner_mismatch_to_409_not_500 (m: red w/o fix, green with).
- Minor, accepted+fixed: premerge.sh counted pytest in-test skips/xfails as PASS → now SKIP (INCOMPLETE exit 2).
- Major, deferred → FIX-QUEUE F1b: session_bootstrap._adopt_into_account (Incogniton/Playwright captures) routes by headers.user_id with no auth_id check. Pre-existing path, not touched by F1 (i).
- Minor, deferred: enrollments.submit_enrollment curl branch has no owner check; save_connection sink refuses it later (i).

## F1 merged
- PR #84 squash → b9a8c516 on claude/commercial-r0-r3-20261111 (m). Head 3edb4001: 16 files, +261/−17 (m).
- premerge: 42 PASS, OK (0 skipped, 2 known-red), exit 0 (m). CI billing-locked (annotation verified, m).
- Lesson: touching server.py made bare-word selection pull 11 unrelated base-red files (all measured identical on a253c84c). Selection now by dotted import path.
- Premerge needs `PY=<main checkout>/.venv/bin/python` — worktrees have no venv.

## F2 merged
- PR #85 squash → 6ca2172a on claude/commercial-r0-r3-20261111 (m). Head fee1346c: 10 files, +210/−59 (m).
- rwl: r1 (Opus + Flash) gave 9 accepted, 2 deferred, 1 rejected (/tmp/rwl-f2/FINDINGS.md); r1 fixer 64ec8598. r2 delta: N1 (config gate before proxy lookup), N2 (proxy required in normalize_browser_payload) fixed by me at fee1346c, with the N1 test measured red→green and promoted. N3 → F2c. N4 (TEST-CHANGE line) went into the squash msg. The Flash 502 point was re-raised and re-rejected (upstream failure = 502).
- premerge at fee1346c: OK (0 skipped, 2 known-red), exit 0 (m). CI billing-locked (annotation verified, m). base = team branch (m).
- Lesson: premerge.sh takes no base arg (an arg is read as a test path → MISSING → FAILED).

## F3 merged
- PR #86 squash → bdc6ad2b (m). Head 009bac1b: 4 files, +74/−41 (m). W3DC-06 + W5BB-7 + FL-3 (no Sign/checksum/start/end echo) + W3DC-04 (-H Cookie form).
- rwl r1 (Opus + Flash) FIX-THEN-MERGE: duplicate Sign parsing → `_sign_sample` helper; Cookie header left when -b given; loose tests; unused `as e`. All fixed. r2 delta: both MERGE (m).
- premerge OK (0 skipped, 0 known-red), 26 PASS (m); ran on the tree committed 3s later (i). CI billing-locked (m).
- Lesson: a resumed implementer (SendMessage) is cheaper for r1 fixes; it kept its context.
