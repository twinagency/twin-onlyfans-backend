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

## F0 r2/r3 + F5 r2 (00:xx 11-13)
- F0 r2 (Opus): budget=0 → GREEN w/o running; ?dsn= DB bypass. Fixed 15b78beb (299 LOC, fast GREEN 42 s measured by worker).
- F0 r3: Opus all r2 closed + 1 test string; Flash extra hardening. A: comma-host case, budget cap, evidence-write INFRA, guard inside db_checks (+ M4/umask if LOC fits). R: fast all-DOWN = note (owner spec). D → F0b: premerge ONLYFANS_TEST_PG_URL loopback check, full skip-only detection, test gaps.
- F5 r2 cf145ec4: Activate after stop writes policy, runtime stays off; uses TextAuthority._configure_in_session. Behaviour change accepted: Activate refuses fansapi→native (existing safety invariant text_delivery.py:312); pin test requested.

## F0 merged (#89 → 91e32acc)
- rwl 3 rounds (Opus+Flash). Orchestrator-measured: harness 35 passed; `check.sh fast` GREEN wall 39 s (premerge + promoted + live smoke). CI billing-locked (annotation checked) → merged on local proof. Team head was 96609e23 (no rebase). Local target ff 96609e23..91e32acc; no wt on target.
- Merge gate from now: `scripts/check.sh fast` GREEN (env: CHECK_ENV_FILE, CHECK_DB_URL stack PG :55432 read-only, ONLYFANS_TEST_PG_URL throwaway PG). Merges: 6 (F1 F2 F3 F2b OWN-b F0).
- F5 r2 Flash MERGE / Opus FIX-THEN-MERGE (F5-21 ordering test, F5-22 comment) → worker.

## Owner 23:4xZ THROUGHPUT + 00:0xZ TRIAGE ON FLASH
- F0c (#90): 3 env-gated release-rehearsal tests → premerge-known-red (still run by full; run-service-tests.py never reads the list, grep). premerge OK 0 skipped 3 known-red (measured). Unblocks the check.sh fast gate for F5/F4. Flash solo review re-run (1st interrupted).
- Re-sort: started on Opus, stopped per 00:0xZ; handed to Flash (delegate, worktree, read-only; PG :55650 /tmp/pg-triage). Opus leftovers: PG stopped+removed; /tmp/fix-dedupe detached worktree kept (4 untracked scratch test copies, not backed up → rule: keep).
- Target check: only our 6 merges since a253c84c; claude/commercial-final-20261113 + commercial-integration-4 do NOT contain target (mb e8e90954) → target unchanged.

## F0c merged (#90 → 29f28729), 2026-11-12T23:45Z
- Flash solo: MERGE; lows: unconditional deselect (→F0b), "full runs them" overstated → reworded. check.sh fast GREEN 32s (measured).
- F5 rebased on 29f28729; first fast run RED rc=143 = external SIGTERM at 60s (F4 premerge died same moment; inferred external kill, no test failure). Rerunning. F4 worker resumed with check.sh fast gate.
- Merges: 7. Fable consult due at 8.

## F5 merged (#91 → a1151b1f) + live batch 2, 2026-11-13T00:0xZ
- check.sh fast GREEN 882s at e4d64f5b (measured; 3 parallel test streams; budget 900) → near budget: split changed-area vs full promoted queued (F0b), no tests dropped.
- Stack restart under STACK.lock: /tmp/stack-live bdc6ad2b → a1151b1f (api, worker, sign-in; dashboard/broker untouched; no migrations in range). Sign-in needed node_modules symlink in /tmp/stack-live.
- check.sh live GREEN 1s (measured; evidence in .local-dev/evidence/fix/live-a1151b1f/).
- LIVE-CHECK W6SI-18 (#88): :8479 LISTEN 127.0.0.1 (was *:8479) (measured).
- OF 429s since restart: api 0 / worker 0 (measured; logs truncate on restart).
- Merges: 8 → Fable consult.

## Fable consult @8 merges (2026-11-13T00:1xZ) — continue
- Gate time: before splitting/capping, time a gate solo-ish → contention ⇒ serialise gate behind a lock; suite size ⇒ split (changed-area as PR gate, full promoted + touched-area before EVERY stack move; not nightly).
- Throughput lever = gate. Flash-only ≤50 LOC only outside risk classes (secrets/egress/tenancy/send/money). No 3rd worker. Batch only same root cause, same file, ≤300.
- 22 untested + 25 drift-unknown: read-only repro pass (no test-only PRs); red test written when fix is picked.
- Order: blk reachable with sending off before send-gated; live sec-money maj (egress/tenancy) > latent blk. G01 first, G03 after F4.
- Mapping check: old F6 (W3RO-01+W3RCB-01) = G03+G04; F2d corrupt proxies.json = untested follow-up (no red test) → live egress, rank with sec-money maj when picked.

## F4 (W6HR-1) gate
- Worker: 8ab70d0e, 9 files +159/-10, red→green 5 cases (measured). check.sh fast RED: 900s budget hit under 3-stream contention (38 PASS, promoted tests never reached).
- Pre-existing base reds found (measured at 96609e23 by worker): test_contextual_release_pg::test_mode_save_preserves_scope_and_populated_rollback_retains_unknown, test_engine_defects_pg::test_r1_paused_plain_replies_cannot_pitch_price_or_claim_purchase, test_history_capacity_turns.py (3 errors); test_onlyfans_native_connector_pg 31/31 Errno 42 loopback (macOS env).
- Timing run with CHECK_BUDGET_S=3600 started (measurement, not gate). rwl r1: Opus reviewer + Flash.

## 2026-11-13 01:3xZ: lanes
- F4 r2 e77ad4a1 (15 files +270/−29, migration 0134 skipped_input_id): rwl r2 running (Opus + Flash).
- G01 60b7e89d (W3FA-03 webhook _resolve scoped per account; W2S-02 unreadable ownership evidence → Refused; 6 files +135/−8; gate GREEN 780s at 29f28729, with 2 env known-reds: commerce PG_BIN, media SO_ACCEPTCONN): rwl r1 running (tenancy/money = full rwl).
- F4 timing run (CHECK_BUDGET_S=3600, little contention): >28 min and still in m* files at 01:30 (m). The cause is suite size (touched-area of store.py), not contention (i) → F0d worker: parallel per-file runner (CHECK_JOBS), or a fast/wide split; also known-reds the 4 a1151b1f base reds.
- G02 (W2SC-02 pilot stop refused → rollback, LIVE stop) worker started, disjoint from F4/G01/F0d.
- Workers: F0d, G02 (2/2).

## 01:4xZ
- Over the 2-worker cap (G01 r1 fixes + F0d + G02) → G02 paused: WIP commit 86327a35 on fix/G02-pilot-stop-refused (pilot_scope.py +5/−2 + test; neighbour suites + gate pending). PG 55651 stopped.
- G01 rwl r1: Opus + Flash FIX-THEN-MERGE (/tmp/rwl-g01/FINDINGS.md). Decision: strict raise only on engine + Moxy (Moxy ValueError → Refused, per-event hold); relay callers get an explicit lenient mode = base behaviour → relay gate. Sent to the G01 worker (rebase onto a1151b1f, PG 55654).
- F4 rwl r2: Opus + Flash FIX-THEN-MERGE; r3 list in /tmp/rwl-f4/FINDINGS.md (same-page tip helper after record, migration IF NOT EXISTS, unfiltered ids, tests; W6HR-2b tip-only re-read deferred). Split into PR-a column / PR-b logic if > 300 LOC. Waits for a slot.
- F4 r1 timing run: RED at the 3600 s budget, 140 files, unfinished (m). test_native_pilot_service.py hung ~45 min (send-check-failed under the dead proxy) → a per-file timeout goes in F0d.
- More base reds at a1151b1f (m): campaigns_pg 15 (identity None :99), moxy_input_supersession 1, moxy_vault_cross_lineage 1 (env: hardcoded /home/ronan path). multicreator_load passes at base in isolation (58 s); failed at 181 s under load (flake or F4; recheck at r3). → F0d.

## 02:xxZ G01 split
- G01 r1 fixes (e31e9e57): 14/14 green (m). Gate RED only on base reds (moxy_input_supersession; multicreator_load contention-flaky at base), 1746 s at budget 5400 (m). Total 356 LOC > 300 → split by root cause:
  - G01a fix/G01a-webhook-scope 43e8ef5a (W3FA-03; 3 files +182/−7; 6/6 green, m)
  - G01b fix/G01b-ownership-strict d7aeed45 (W2S-02 + Moxy per-fan hold + relay lenient; 6 files +154/−13; 8/8 green, m)
- rwl r2 on both (Opus + Flash) running. F4 r3 worker started (slot 2; F0d is slot 1). native_pilot_service: base red, 62 s (m).

- 02:5xZ G01b r2 fix 75f7b602 (/tmp/fix-g01b): G01R2-01 held evidence event -> diagnose + one moxy_technical case (method _evidence_hold); G01R2-02 test: 2nd step no retry, no model call, case once. 29/29 g01b+moxy_integrated+w2_sale green (measured); mutation drop-case -> red (measured). Awaiting Flash r2 (4f1e1aaf), F0d, F4 r3.
- 03:2xZ Flash r2 G01: FIX-THEN-MERGE both. Fixed: G01a 182a37fe (FLG2-02/04; webhook 170/170), G01b 06167d9a (FLG2-01 high: receipt path regression; red at 75f7b602, green after; moxy+text_delivery 116/116). D: FLG2-05 disclose, FLG2-07 relay gate. Flash r3 delta review running (780493b0).
- 03:4xZ MERGED G01a #92 = 91547788 (gate GREEN 481s measured; merge #9). Local target ff to 91547788; no worktree checked out on target. G01b rebased (promoted list union) -> 591c391e, gate running.
- 04:1xZ F0d worker done (bd0f25f8; fast 40s own / 658s on F4 diff, measured); I dropped loose commercial_full_path entry (3dc98ef1). Opus F0d review FIX-THEN-MERGE (2 blk: lint var clobbers pool dir; deselected+errors masked) → /tmp/rwl-f0d/FINDINGS.md, sent back to F0d worker; Flash F0d (18ebeb16) running. G02 finished by me: 0ba586cd on 91547788, red at base 3/3, green + neighbours 49/49 (measured); rwl Opus+Flash (7f5d6057) running. G01b gate running.

## 2026-11-13 G02 / G01b gate
- G02-03 fixed by orchestrator: `_assert_stopped` asserts audit row after = min/max clamp of before (15fc3e89). Throttle drift not parametrized: DB floor 1 == scope limit 1. Green fix / red base (measured, PG 55651).
- G01b gate FAIL test_moxy_input_supersession_pg::test_explicit_retirement_keeps_old_inputs_in_history_only: red at base 91547788 too (`moxy_event_key_conflict`, measured) → base red, already in F0d known-red; add to G01b known-red, rerun gate.
- G02-01 (maj send-safety) + G02-02 (minor) queued in FIX-QUEUE.
- F0d r1 ceb23b6b (worker): F0D-01..09 + FLD-01..06 fixed, own fast GREEN 53s (m), +243/-36. history_capacity_turns dropped from known-red (= team base red; will FAIL any gate touching it). Flash delta re-review launched.
- Slot 2 → G04 (W3RCB-01 rest, native_protocol history_text) worker via delegate_task Claude3 Opus, PG 55654. Disjoint from F4/G01b/G02 (i).
- G01b gate1 RED 1762s (m): 2 FAILs both red at base 91547788 (m) → known-red 6b9b0aa5, gate2 running.
- F0d: Flash delta FLR-01 (rc 124 own-exit deferred) + FLR-02 (HUP) fixed by orch 5fbd1a23 (red old/green new, m). Rebased → 83699ddf, fast GREEN 52s (m). **#93 merged 5bc2abd9** (merge 10). Local target ff'd.
- Gates restarted on new harness (default 900s budget, CHECK_JOBS=2 each): G01b rebased 843485e0 (known-red union + dedupe), G02 rebased. Old gates stopped.
- Stack restart: 2 merges since a1151b1f; due after G01b (3).
- G02 gate GREEN (11 deferred) 623s (m); deferred run by hand. G01b gate INFRA 687s: 6 opt-in transcript skips (m) → rerun ALLOW_SKIP=1 + 8 deferred by hand. F0e queued.
- 2026-11-13: G02 merged #94 e5903d1b (fast GREEN 11 deferred 623s; deferred 10 pass, u3_readiness 6 red = base, /proc boot_id macOS). Local target ff'd. G01b deferred: commerce_pg `test_encrypted_restore_...` red at base e5903d1b too (KeyError ONLYFANS_TEST_PG_BIN, env); media_pg = known-red SO_ACCEPTCONN. Merges since stack a1151b1f: 3 (G01a, F0d, G02) → restart due.

## G01b merged (#95 → 7e5ac8e4) + live batch 3, 2026-11-13T03:2xZ
- G01b: fast (ALLOW_SKIP=1) GREEN 640s (m); rebased on e5903d1b (promoted-list union), promoted+G02 tests 12 passed (m). Merge #12.
- 429s over a1151b1f window: api 0 / worker 9 (all read `chats`, provider_rate_limited; m). Logs copied to .local-dev/evidence/fix/live-7e5ac8e4/.
- Stack restart under STACK.lock (released): /tmp/stack-live a1151b1f → 7e5ac8e4 (api, worker, sign-in; dashboard/broker untouched; no migrations in range).
- check.sh live GREEN 1s (m). Sign-in still LISTEN 127.0.0.1:8479 (m). Worker post-restart: 29 cycles, 0 Traceback/ERROR/429/evidence_hold (m).
- LIVE-CHECK G01a/G01b/G02: no owner-mismatch capture or pilot stop exercised (would write; read-only rule) → code at running sha only (i). F0d harness n/a.
- Fable consult due (12).
- Fable @12 (03:3xZ): seam fix (a) errno-gated (ENOPROTOOPT only, not platform), 2 red tests; re-run masked loopback PG files at head before G04; F6 + F2d already queued (F6 waits F4, recent_inbox.py). 429s: 9, one account, `chats`, evenly spread (m) → no retry loop (i).
- FLG2-05 seam fix /tmp/fix-seam (fix/FLG2-05-loopback-enoprotoopt): new test 3/4 red at base e5903d1b, 4/4 green (m). Masked files at head+fix (m): native_connector 31/31, media 38/38, loopback 47, worker 23, worker_media 3, native_loopback 1, egress 51 → no hidden regression from 12 merges. http_pg::test_real_authenticated_read_path now fails later on env (needs ../twin-dashboard/node_modules), base still ENOPROTOOPT. media_pg pin removed. Gate + Flash running.
- G04 rebased 4bd228b5 (list unions); Flash MERGE + 1 minor fixed (3 must-refuse cases) → 522507d2. Merges after seam.
- MERGED FLG2-05 #96 → 75546f24 (fast GREEN 135s, re-run after Flash minors 140s; Flash MERGE + 2 minors fixed, mutation-checked; m). Merge #13. Fresh worktrees need creator-sign-in/node_modules symlink or test_signin_bind_loopback skips → INFRA (env).
- G04 rebased 05062f35/a9ae5396 on 75546f24; gate running (PG 55651).
- Promoted-list append conflicts on every rebase (union each time) → F0e candidate: keep list sorted or one file per PR.
- MERGED G04 #97 → 1d2c5f36 (fast premerge OK 427s; live leg DOWN = dashboard :3001 stopped by another lane, not mine; `check.sh live` re-run GREEN after it returned; Flash MERGE + 1 minor fixed; m). Merge #14. Pushed as fix/G04-history-text-r2 (old branch non-ff, no force). Known-red pin `test_newer_valid_inbound_survives_one_bad_older_row[old-unsupported-formatting]` → unpin with F4 `_skip_reason` follow-up. Stack 7e5ac8e4: 2 merges since.
- 03:5xZ G05 (content safety ARMED: safety_detect / safety_context / vision; disjoint with F4) delegated → [G05 content-safety fix] Claude3 opus high, worktree, PG 55650, red tests from /tmp/fix-sweep. Risk = send-adjacent → rwl-by-risk before merge. F4 r3 still running.
- G05 worker done: fix/G05-content-safety 944cf94b, +148/-17, 19 red→green, 16/16 mutations, fast GREEN 164s (m). rwl r1 (full tier): structure + correctness REQUEST CHANGES, Codex 3×P2 false-positive minor (m), Codex DA 5 P1 (2 pre-existing: output_violation unwired, minor under shadow mode). Fable@15 (hard decision): split — PR1 narrowed native + engine exemption + vision, distress/consent back to stripped text; PR2 shared minor module + parity table (queued). Owner gates before send re-enable: (b) explicit minor disclosures bypass shadow? (c) wire output_violation. Fixer r1 running (PG 55661). 55650 held by another lane's PG (/tmp/pg-triage) — not mine.
- 05:4xZ F4 r3 done (worker): 2 branches split 1a column/migration 0134 (55 LOC) + 1b logic (299 LOC). Old gate 5400s budget-RED (rc 124); remainder 3101s, all FAILs red at base a1151b1f (m). Rebased by orch onto 1d2c5f36 → 17621fff / 22a08de6 (promoted-list union ×2). Final checks running on frozen /tmp/rev-f4: Sonnet delta verdicts (r2 findings /tmp/rwl-f4-r2-findings.md), Codex final, Flash final. Gate on rebased head running (PG 55652). FL2-02 deferred (W6HR-2b, disclose). Stack restart will need alembic 0134 on stack DB via of-up.sh under STACK.lock.
- G05 DeepSeek r1: +2 majors (16f/"just turned 16"; exemption looks only before noun) held → fix round 2 (/tmp/rwl-g05-flash-r1-pending.md).
