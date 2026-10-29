# Claude handoff: roster sync and fan-memory backlog

Jesse requested that GPT stop development, push remaining work and hand this task to Claude on 29 October 2026. No production mutation was performed for this handoff. Existing background workers have not been stopped.

## Start here

- This task: [PR #49](https://github.com/twinagency/twin-onlyfans-backend/pull/49), branch `gpt/roster-sync-repair-20261029`.
- Separate memory implementation: [PR #47](https://github.com/twinagency/twin-onlyfans-backend/pull/47), branch `codex/automatic-onlyfans-profiles-20261029`. Last inspected head: `d22ec7db35f7c06adee5012ed987a9cb7104f3fd`.
- Read [RELEASE-20261029.md](RELEASE-20261029.md) and [README.md](README.md) for the sync release, tests, scoped deployment scripts and retained rollback resources.
- Read PR #47's `deploy/profile-learning/README.md`, `runtime-overlays.json` and `upgrade-current-runtime.py` for the independently deployed memory code.
- No unfinished runtime patch remains in this task. The last two turns performed read-only memory diagnosis only. Queue-priority and extraction-error fixes discussed below have NOT been implemented.

## What is already implemented and pushed

The sync repair retains fan names/usernames/avatars during archive discovery; schedules bounded identity refresh for every known fan without a 37-fan cap; enrolls later-discovered fans into history recovery without resetting existing cursors; prevents recent failures starving other sync lanes; and exposes bounded, secret-free provider failure classifications.

Live follow-ups increased GET deadlines from five to ten seconds, capped native history/discovery pages at 20 records while retaining the 1 MiB response bound, and normalized the observed bare-br line-break dialect only for incoming historical text. Single-attempt writes and existing outgoing validation were preserved.

Core source `0b8fecf7ddde4b986b3df4960d206bbdc639f780`; GET timeout `f6e0576d79d3d49ac427046fbbed952b000e48c7`; history format/pages `b85857adf1c72dcc5c08497351b757442e736161`; release evidence `a0e2a88cac86cd1696af0102f6ef763fa5b57eef`.

All 17 changed files in PR #49 were checked against the local candidate before this handoff. Runtime code/tests matched GitHub exactly; the local release document differed only by an extra terminal newline. Other copied test dependencies were unchanged repository files, with terminal-newline differences only.

## Release reconciliation is essential

The sync release evidence records images as of 13:42 Brisbane. It is not a claim that its API/sender images remain the latest deployment.

PR #47 was subsequently updated by the separate memory task. Its inspected release report states API/learner image prefix `3d2825f5cfe8`, sender `1fb5e52bc4d3`, preserving sync worker `946e343a07d2` and dashboard source `f656663`. That report is a separate task's evidence; this handoff did not perform another live image inventory.

The last read-only database diagnostic inspected the running learner source hashes:
- `onlyfans_runtime.profile_learning.worker`: SHA256 `8122ae28088b8f2ba17a72c33678aa560180b2dd956794aaf21f81495d9e252d`.
- `onlyfans_runtime.profile_learning.model`: SHA256 `e87e55bce941661d6db060e01bb2a5142309153199d4cf022efeb8d46284f22d`.

Establish current images/source/configuration before editing or deploying. Do not deploy either PR branch wholesale over newer API, sender, dashboard or worker work. No main/master merge or write occurred in this task. Schema after the sync release was `0099_fan_identity_sync`.

## Latest memory evidence: approximately 13:58 Brisbane / 03:58 UTC

These are scoped creator/fan records, not globally deduplicated people. Snapshot reads occurred in separate read-only transactions while import continued.

| Creator | Account | Enrolled memory jobs | Checked jobs | Fans with usable stored incoming messages | Fans with incoming-message receipts | Current saved memory items |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Nicky Baby | of-176774528 | 12,506 | 72 | 268 | 3 | 17, all for one fan |
| Katie Price | of-210806616 | 34,254 | 78 | 214 | 0 | 0 |
| Maile | of-323358988 | 9,855 | 81 | 95 | 1 | 3, all for one fan |
| Alva Jay Free | of-87030857 | 3,923 | 81 | 123 | 0 | 0 |
| naomii | of-99421145 | 7,993 | 75 | 219 | 9 | 0 |
| Total | | 68,531 | 387 | 919 | 13 | 20 |

Maile also had one expired memory item. The two current-memory fan profiles are not the entire roster and 20 is not a configured cap.

Definitions:
- Enrolled means a row in `of_profile_learning`, not a completed analysis.
- Checked means `checked_at IS NOT NULL`, including checks that found no learnable messages.
- Receipts include outgoing, blank and withdrawn messages that are checkpointed without model analysis. Receipt counts must not be advertised as completed AI memories.
- Usable stored incoming messages means nonblank, nonwithdrawn inbound rows with applied ingress evidence matching the current account binding. This count does not prove that OnlyFans has no more messages or fans.
- The sync repair's earlier 395 successful profile checks concern fan identity fields, not AI-generated memory.
- A completed learner pass covers currently stored messages, not necessarily complete provider history.

There were 7,833 usable stored incoming messages across the 919 fan scopes. The learner had incoming receipts for only 13 fan scopes and many more outgoing receipts. Its work was progressing, but fleet memory creation was far from complete.

## Confirmed blockers and remaining work

1. **Alva and Katie configuration:** both have `account_ai_config.model = NULL` and `model-credential-missing` jobs. There is no saved matching credential available to the learner for those accounts. Do not silently copy another creator's credential or change models/budgets. Complete the supported configuration-owner flow.
2. **Unfinished provider history:** all five accounts still had incomplete history at the sync release observation. Continue genuine cursor-based import and verify coverage; do not stamp completion or reset cursors to make the warning disappear.
3. **Queue efficiency:** only 387 of 68,531 jobs had been checked at this snapshot, and just 13 fan scopes had incoming receipts. Inspect scheduling, repeated full-roster seeding and database costs. Prioritize available fan evidence while keeping fair service across creators and avoiding starvation. This is a proposed improvement, not a diagnosed performance root cause or an implemented patch.
4. **Deferred analysis:** Nicky had two deferred fan jobs. Maile and Naomi each had one deferred creator-draft job. The public code `profile-analysis-deferred` collapses multiple possible exceptions; the exact underlying causes have NOT been established. Add safe error classification or inspect bounded local diagnostics without exporting private bodies, prompts, credentials or raw provider responses.
5. **Do not equate model calls with saved facts:** 26 `gen_info` calls since 02:45 UTC had status `done` (Nicky 11, Maile 1, Naomi 14). This denotes settled model calls, not 26 completed profiles. Sampled Naomi responses contained valid JSON with an empty facts list. Review evidence/extraction quality using authorized sources, without fabricating memory for fans whose messages disclose no personal information.
6. **Acceptance:** verify new saved memory for multiple real fans on each configured creator, verify inbox/native/FASTT retrieval, and report separate counts for pending, blocked, source-empty, processed and complete-history coverage. Existing human notes and protected memories must be preserved.
7. **Separate sync issue:** `ledger_unavailable` worker events were observed earlier and remain an outstanding operational investigation. The history changes do not establish that all CRM services are error-free.

Existing OpenRouter/DeepSeek configuration was observed for Nicky, Maile and Naomi. Their daily caps were unchanged (1000, 100 and 1000 cents respectively). Alva/Katie caps were 100 cents each. This task made no model calls or credential changes during the final diagnosis.

## Reproducing the read-only diagnosis

`memory_diagnosis_readonly.py` preserves the exact diagnostic used for the final snapshot. Run it in the existing learner container with its already-configured `DATABASE_URL` and service imports available. It sets PostgreSQL transactions to read-only and uses statement/lock timeouts. It reads aggregates, code hashes and response structure only; it never calls the model/provider or writes application rows. It does not print credentials, fan message bodies, prompts or response content.

Example from an authorized deployment host:
```sh
docker exec -i twin-onlyfans-profile-learner python -B - < deploy/roster-sync/memory_diagnosis_readonly.py
```

The diagnostic is intentionally dated to this incident. Adjust its `called_at` cutoff for later investigations. Read-only SSM evidence: `62662d40-be1f-48a6-8787-e6b7eddd4269`; preceding status check: `6d0bfdff-5a65-417f-8d4d-3bd0b46b2a89`.

## Validation already completed

Core sync: 217 selected regressions including disposable PostgreSQL, then 40 focused final checks. Timeout follow-up: 109 checks. Final history/wire follow-up: 124 tests and 10 subtests. These suites overlap and are not additive unique counts. Exact-image smoke, schema, API health and service checks passed at release. PR #47 reports its own separate validation.

The handoff adds documentation and the already-executed read-only diagnostic. It makes no runtime changes. Private operational files, keys, database backups and raw private logs remain outside GitHub.
