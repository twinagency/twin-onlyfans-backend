# OnlyFans agency activation record — 28 October 2026 (AEST)

Written by the Claude release session. Facts below were read from production over AWS SSM or from GitHub at the times stated. Anything not proven is marked OPEN.

## 1. What is live

| Component | Source commit | Image | Notes |
| --- | --- | --- | --- |
| API, worker, FASTT sender | `7f4eaf0bcb0b461300a0301330acd773f2b3d8f0` (`codex/onlyfans-of-b1`) | `sha256:bc2a3dc7e012…` | built on the box from `deploy/onlyfans-compatibility.Dockerfile` over base `sha256:174d20ef…`; 718/718 `service/**/*.py` hashes matched source |
| Managed proxy | `293b23d` (unchanged) | `sha256:b4c3ec13…` | not restarted |
| Creator sign-in | unchanged | `sha256:b63d926b…` | not restarted |
| Twin dashboard (`next-app`) | `2d0653343afddca830a9734dcc87e5eef4b2cde4` (`release/twin-20261028`) | `sha256:d5d4cc71…` | runtime overlay on the previous image; rollback = `/var/tmp/twin-allfans-20261028/compose.before.json` (image `fccf205a`) |
| FLIRT dashboard | `2e5184d2e8ad0f5582002142f9f98785108ffff5` (merged in `release/flirt-20261028` via #308) | `sha256:9305291f…` | rollback container `flirt-dashboard-before-remove-of-card-20261028` |

Schema: `0098_ledger_exclusion` (additive: one empty table `of_ledger_exclusion`; migration took 5.9 s).
Pre-migration backup: `/var/lib/twin-release/claude-20261028/pre-0098-20261028T002821Z.dump` (pg_dump custom, 263,533,169 bytes, 162 table-data entries, sha256 `7b4df3da…2852aa6`).
Release evidence on the box: `/var/lib/twin-release/claude-20261028/` (`release-proof.json`, compose/inspect snapshots).

## 2. Scope and controls

- Sender `ONLYFANS_FASTT_SCOPE=db:*` (API/reader already `db:*`). Sender and API ownership now agree.
- Supervisor at 00:36 UTC: Nicky `of-176774528` and Naomi `of-99421145` running all-fans children; Maile `of-323358988`, Katie Price `of-210806616`, Alva Jay `of-87030857` held `runtime-disabled`.
- Only conversations with `mode='automated'`, takeover `none` are answered (4 each on Nicky and Naomi at deploy time). A missing control row is never served.
- New fans are never auto-enabled (`NEW_CONVERSATION_AUTOMATION_LOCKED = True`, both writers). Dashboard "enable all fans" enables existing chats only.
- `MAX_ACCOUNTS=64` now counts eligible accounts only.
- Maile paused through the audited account control before the scope change (owner decision: no mass replies to her 610 previously automated chats). Command key `143db80f…ee28fef`.
- Daily limit unchanged: `FASTT_FAN_DAILY_REPLY_LIMIT=2000`.

## 3. Money treatment

- Per-fan commerce hold: a fan with an unlinked paid `ppv_message` since the current binding gets no paid card, discount resend or post-buy offer on any path (native sales, media delivery, FASTT `>>OFFER`, postsale). Ordinary chat continues. Account-wide clauses of `held()` unchanged.
- Audited `exclude_unresolved_ledger` command exists (only for `provider_message_identity_missing`); NOT yet used. No links guessed, no rows fabricated, no holds cleared.
- `delivery_unknown`: 1 attempt before, 1 after (still held, not resent).

## 4. Identity rule (OnlyFans only)

- Prompts no longer say "AI chat clone"; rule = never mention AI/bots/clones/automation, never claim to be human.
- A fan asking "are you a bot / AI / real" gets no AI reply: chat paused via `Store.control` (actor `identity-guard`) and flagged `requires_attention`.
- Send boundary holds any bubble that describes itself as AI/bot/clone (self-description patterns, not every mention of the word "AI").

## 5. Live proof (Nicky, test fan 601164546, 28 Oct ~00:46 UTC)

| Step | Result |
| --- | --- |
| Fan "Hey baby how r u" → AI reply "im good trouble, still waiting on that pic?" | PASS, provider message id `11822069385884` |
| Fan "A y a bot" → no AI reply, chat paused + flagged | PASS (first via runtime-stop fallback; import bug fixed in `7f4eaf0`) |
| Operator takeover (`request_takeover`, revision 5) | PASS |
| Manual reply as creator | OPEN — refused `history_quarantined` (pre-existing: 141 quarantined ingress rows on Nicky since 24–25 Oct, 1000 on Maile). Audited resolution in progress. |
| Hand-back (`release_takeover`, revision 6) | PASS |
| AI resumes after hand-back | OPEN — needs one more fan message |
| New creator sign-in → import, paid $3 item | OPEN |

## 6. Capacity (offline harness, fake provider and model)

- One process per creator; each creator is served serially with a 20 s throttle → hard ceiling ≈ 3 replies/min per creator.
- Valid (x3 time scale) runs, 4 creators × 250 fans: at 1.5 msgs/min per creator all p95 29–89 s; at 2.5/min two of four creators exceeded 120 s p95 (145 s, 234 s).
- Supported tested capacity: ~4 active creators at 1.5–2 inbound msgs/min each. Not measured: real model/provider latency, prompt-building CPU, >4 creators at valid scale, the 64-creator cap.

## 7. Rollback

Forward-fix only for the backend: old images cannot run on schema 0098 (`check_schema`). Code-only regressions roll back to `twin-onlyfans:claude-69025b11` (`sha256:0ae6ad56…`) by editing the three service images in `/etc/twin-onlyfans/media-agency-release-compose.json` and `docker compose -p twin-onlyfans … up -d --no-deps --no-build --pull never api worker fastt-supervisor`. Dashboard and FLIRT rollbacks listed in section 1. Never restore the pre-0098 dump over newer receipts.

## 8. Open items

1. Resolve Nicky/Maile quarantined ingress (blocks manual replies) — in progress.
2. AI-resume proof after hand-back; new-creator import proof; paid item proof.
3. Maile: decide what to do with her 610 old automated chats before resuming her account.
4. Owner of chats flagged by the identity guard.
5. Repeated `twin-controls-error` lock timeouts on Nicky's sender (benign so far; watch).
