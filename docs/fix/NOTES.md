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

## Live confirm F1–F3 (STACK.lock 21:29Z → released)
- Stack: api+worker restarted onto bdc6ad2b from detached worktree /tmp/stack-live via patched of-up copy (init.sh copied in; it exists only on the takeover branch). api/worker were down ~2 min on the first failed attempt (m). Dashboard/sign-in/broker untouched (m). Evidence lives in .local-dev/evidence/fix/2026-11-12-F1-F3/.
- HR-1 (pre-restart worker log, 943f3851, 19:47→22:29): 0 history_rows_skipped, 0 NativeProtocolError, 0 history_not_current (m). W6HR-1/-2 did not fire in this window.
- capture-from-curl, synthetic curls (x-hash and static-param supplied, so no OF egress, no writes):
  - F1 wrong owner → 409 owner_mismatch (m).
  - F3 malformed sign → 400, no echo; non-hex checksum → 400, no echo (m).
  - W3DC-04 `-H Cookie` form → 409 owner_mismatch, so the header is now parsed (m).
- Log grep: synthetic secrets 0 (m).
- W5BB-L1: unauth BB status → 401 (m). W5BB-L2: BB key/signingKey/connect URL in api log → 0 (m). W5BB-L3: key present in api env, as expected for BB start (m).
- F2 (proxy on capture fetch): confirmed only by the running sha, inferred, because no safe probe exists (a probe would cause OF egress).
- Probe gotcha: connection routes need the header `X-OnlyFans-Connection-Version: of-connections.v1`, else 400 unsupported_schema.

## Fable consult @F3 → REDIRECT (accepted)
- Order: HR-1 live first (done: no fire); check F2b reachability; then F5, then F6 (+W6HR-2/3 if HR-1 had fired; it did not); F4 later as shape (b) only.
- F4 shape (b): MessagePage carries skipped fan-row ids; a store helper bumps input_revision above high_water; all 4 page-commit sites call it. Hard gate before sending is re-enabled. Reject (a): high_water=0 on first import would refuse all tips. Tip-note text loss queues behind D3.

## F2b merged
- F2b is reachable: Dockerfile CMD = `service.server:app`, so the paste-curl `/admin/session/bootstrap` is live in deploys (i). The legacy relay is not running locally (m).
- PR #87 squash → 9124b50d (m). Head b972eef3: 5 files, +146/−8 (m). from_curl 2313.js now uses `fetch_asset` (curl_cffi, capped, no redirects) via the proxies.json proxy keyed on curl user-id. Rules: assigned→proxied, unusable entry→refuse, none→explicit None. Shared `_check_ids` (one REV regex). Route maps Refused→502.
- rwl: r1 Opus+Flash FIX-THEN-MERGE (/tmp/rwl-f2b/FINDINGS.md); FL-01 get_for_session fallback rejected (account-first binding). r2 → re-export Refused + pin narrowed catch (red m). r3 Flash MERGE.
- premerge (throwaway PG :55645): OK 0 skipped 0 known-red, 40 PASS, exit 0 (m). CI billing-locked (m). base = team branch (m).
- Implementer bug I caught: proxy lookup keyed on `account_id or user_id`, but session binding is by user_id (session_bootstrap :496). Fixed and pinned (red m).
- Live: NOT confirmable safely. The legacy relay boots the automation executor, so there is no local run. Proof = tests at the transport seam (m) + merged sha (i).
- Deferred → queue: corrupt proxies.json → "no proxy" → direct (pre-existing, shared with of_client); proxies.py probe → ipinfo.io direct.

## Fable @4 merges → CONTINUE
- Order: F5 (full rwl) → F6 → **F2d** (corrupt proxies.json fail-closed + atomic save; ahead of F7–F11 because it needs no sending to fire and leaks a real IP irreversibly) → F4(b) → F7–F11. F4 remains a hard gate before sending is re-enabled (an HR-1 zero over 2h40m is weak evidence).
- F6: place the retry cap where a skipped-head fan also hits it (bounds W6HR-2 for free); do not widen the PR.
- F2d risk: check that proxies.load is not reached at import/boot//health, so a raise refuses instead of crashing the relay.
- F5: cover every writer of stopped/enabled state, red test through the real Activate entrypoint (one case per stopped state), live proof must arm nothing.
- Ledger: F2 proxy-on-fetch = inferred; F2b = transport-seam tests only. Never "live-confirmed".

## OWN-b merged (#88 → 96609e23)
- sign-in listens on `HOST || 127.0.0.1`; compose sets HOST=0.0.0.0 (expose-only private net). Red `*:52115` → green 3/3 (m). Local dashboard sign-in host = 127.0.0.1 (m). premerge OK 0 skip (m). Flash: 3 → 2 fixed (empty HOST fail-open, lsof skip), 1 deferred (CI never installs creator-sign-in/node_modules → test skips in CI). Local target ff'd (m).

## Owner 22:2x–22:4x applied
- OQ-27: relay gate R1–R3 in FIX-QUEUE (after engine/live items). F0 check.sh = next priority (worker running). F4(b) W6HR-1 worker running (owner (c) high priority > Fable order). F5 awaiting premerge + rwl.
- Worktree cleanup: removed 63 (61 finished rwl lanes + fix-f2b + fix-signin). 3 dirty lanes kept (w2-db-migrations, w2-tests-history, w6-automations-engine-e). rwl/* branches were local-only → backed up first to a verified git bundle under .local-dev/backups (71 heads). No branch deleted (m).
- Scrub (e): BOTH repos PUBLIC (m): twinagency/twin-onlyfans-backend and BlackPhoenixSlo/inflowwkiller. Exact lines (values in .local-dev/evidence/fix/scrub/scrub-list.json only):
  - rhythm.py:833-834 (6 account-id literals, "six live accounts") + :483 creator first name — both repos.
  - media_cotag.py:58 DEFAULT_HANDLE (real personal handle) — both repos.
  - of_client.py:1976 stories_items docstring (4 id-like literals) — team repo only.
  - tests: 48 lines / 9 files / 13 distinct id-like literals (6 trivially fake; 0 equal a rhythm account id) — team repo only.
  - Nothing pushed. History scrub = owner decision (in git since ≤659ea29).

## F0 / F5 status (23:4x)
- F0 b537371d (299 LOC): fast GREEN 28 s wall (worker-m), live GREEN ~1 s. **Incident (m, worker-reported):** while picking endpoints the F0 worker issued ONE manual GET to dashboard `/api/health`, which reads the `clones` table from the Supabase project in twin-dashboard .env (may be prod) and calls Telegram getMe per clone. Read-only, single call, not in harness. Breaks "no prod Supabase" intent → reported to owner; future worker briefs list that route as forbidden.
- F0 r1 Flash FIX-THEN-MERGE (5: DB ?host= bypass, token in traceback, false GREEN w/o bearer/DB, test not promoted, no-timeout budget). Opus pending.
- F5 r1 Opus BLOCK / Flash FIX-THEN-MERGE: stop + recapture → unrecoverable (F5-01). Triage /tmp/rwl-f5/FINDINGS.md: A F5-01/03/04, D F5-02 → F5b. Release-test reds (3) = env-gated, identical at base (m) → INFRA.

## F0 r1 review (23:5x)
- Opus + Flash both FIX-THEN-MERGE on b537371d. Union triaged (/tmp/rwl-f0/FINDINGS.md): safety A = skip→INFRA, partial DOWN→RED, drop CHECK_PREMERGE_CMD seam, strict loopback DB guard (host/hostaddr/service/comma/PG* env), token CR/LF leak; also no-timeout→INFRA, live 180 s cap + statement_timeout, promote test, redirect/proxy tests. Extra tests → F0b if over 300 LOC.
- Worker resumed with the fixes; dashboard /api/health forbidden in brief.
