# Claude takeover: OnlyFans profiles and dated memory

Jesse stopped GPT development/deployment on 29 October 2026 and asked for all completed work to be pushed here for Claude. No new historical-retrieval implementation was started after that instruction. Use this branch as the saved candidate, inspect the current runtime before further changes, and continue the original requirements.

## GitHub and source of truth

- Repository: twinagency/twin-onlyfans-backend.
- PR: https://github.com/twinagency/twin-onlyfans-backend/pull/47 (draft, open).
- Branch: codex/automatic-onlyfans-profiles-20261029.
- Original candidate: 894f39cf1ffe4896f60125de762540c79b349f33.
- Integration commit: 816a7c47ec5ea1fc61dac75849a8df70b6fb7027.
- Latest deployed application-memory code: 8a912627879e2d59d65fe6cca0b21a78a5b2634a.
- Before this handoff, branch head was d22ec7db35f7c06adee5012ed987a9cb7104f3fd, including release documentation.
- All 27 candidate files were compared with GitHub at that head. No discrepancies were found. The eight pre-existing integration files were checked against the API integration snapshot, rather than overwriting them from the older local working tree.

The PR base is codex/profiles-current-release-base-20261029 at b338c0c. It is a snapshot base, not a reconciled main branch. Backend main was absent at the last repository check. Newer concurrent sync fixes were delivered as runtime image derivatives; do not replace production with this branch's complete source tree. The per-service overlays and exact image parents matter.

## Actual deployment, last verified

| Component | Image |
| --- | --- |
| API and standalone learner | sha256:3d2825f5cfe8cc7c305b3c4bf4cbd38506a959ebe23182d0f0b536dc0e0f7f7d |
| FASTT sender | sha256:1fb5e52bc4d3ce027af341b5a6caefbe6bd85883b1560067fdd725e784a39bcd |
| Separate sync worker | sha256:946e343a07d204f5946d1e23003170fcdc458b81314a8a83a7bcb8c014aebfc2 |
| Dashboard next-app | sha256:3e7aff70aa8d10c370c60000fcafd3c3be0e3a65347a3a0719c8412c9f9bca74 |

All were running at final verification. Do not treat these as fresh observations after other agents deploy.

The final API/learner image derives from current sync-release API 3e6e4579fefd31d1736a3a45578d3aff115bfb711f10c287248e236de78112cc. The sender derives from f542dedd0ac57a729f3a67725675a4ea73a53704836482c1e91e93367360bf83. Only evidence.py, storage.py and profile_learning/worker.py were added in the final upgrade after matching all three prior source hashes. This preserved a separate agent's newer sync deployment. Earlier cutover guards refused outdated images before changing services.

Host: i-09a9dd3bd749643a5, twin-dashboard-prod, ap-southeast-2. Runtime staging/rollback record: /var/lib/twin-release/profiles-memory-20261029. Existing active compose: /etc/twin-onlyfans/media-agency-release-compose.json.

The learner is twin-onlyfans-profile-learner, with restart-unless-stopped, concurrency two, 512 MiB limit and bounded logs. It remains running. The compose definition includes profile-learner. GPT stopping does not stop this deployed service.

Schema: existing Alembic 0098_ledger_exclusion plus additive profile tables and the message-change trigger. The ingress evidence index was built concurrently and verified valid. A schema-only backup, private compose/config snapshots and stopped rollback containers remain on the host. Never delete backups or replay an old cutover blindly.

Jesse explicitly authorised this task's direct safe deployment after the earlier PR-only restriction. Deployment used IAM user jesse-gpt. No AWS root or IAM changes. No reply activation, fan permissions, allowances, prices, caps, model choices or sales-rule changes. Keep these protections. Do not copy credentials between accounts. Never touch old Supabase cnnsllgzskqkvdovyiiv.

## Implemented and exercised

- Evidence-backed, scoped observations and structured memories, with dates, event lifecycle, review dates, manual protection and corrections.
- Original-message relative dates and known fan timezone; cancellation/postponement; fourteen-day ordinary-event expiry; no assumption a planned event happened.
- Date-of-birth age calculation and conservative dated age-only statements. Future career plans are uncertain events, not current jobs.
- Long-message chunks with receipts, edited/withdrawn evidence validation and context-question references.
- Full-roster discovery in repeated batches, silent-fan account information, persistent leases/retries, account fairness, active-thread priority, two-hour refresh and daily maintenance.
- Shared compact summary/retrieval in the live inbox repository, FASTT hook and native planner. Eight memories normally, ten maximum, 6,000-byte hard budget; summary at most 250 words.
- Enduring facts survive chatter and a large request archive. Request synonyms merge and at most three requests occupy compact presentation. Requests remain stored.
- New messages make visible coverage pending until processed. Restart maintenance preserves failure retry deadlines.
- Each account's existing credential and shared daily budget. Stored analysis independent of OnlyFans session expiry and reply activation.

The live authenticated profile endpoint returned HTTP 200. The inbox repository returned Conversation summary, Profile learning, History coverage and Creator profile coverage rows. Sample summary: 99 words, eight memories. Real native/FASTT context construction passed on stored fan history without sending a fabricated fan message or generated test reply. Browser visual acceptance has NOT passed; the available browser was at CRM admin sign-in.

## Tests and real-history limits

90 focused cases passed together, including 17 real PostgreSQL cases. After the final career-plan correction, 62 evidence/date cases passed, adding three unique cases: 93 unique focused cases across these runs. Earlier integration also passed 64 existing regression cases and 26 native prompt checks. Compilation and whitespace checks passed.

PostgreSQL cases cover locks/leases, first-budget races/caps, rollback, long chunks, source edits/withdrawal, account/manual isolation, refresh visibility, a 301-memory request archive and actual prompt builders. Full-roster tests discover 1,001 fans; SQLite processes all 505 silent fans in a separate roster. Test PostgreSQL was a disposable Windows laptop server on port 55437 and is stopped. Do not run tests that create/drop databases against production.

Authorised bounded real-history extraction examined eight messages for one fan each on Nicky, Naomi and Maile. It exposed and led to fixes for relative-place confusion, birthplace/residence confusion, request-heavy summaries and future-job claims. These samples are not fleet-wide extraction-quality proof.

Jesse approved existing OpenRouter/DeepSeek processing for Nicky of-176774528, Naomi of-99421145 and Maile of-323358988. Saved route: https://openrouter.ai/api/v1/chat/completions; saved model openrouter/deepseek-v4.1-flash. Reuse matching account credentials and existing budgets only. No keys are in this handoff or PR.

A canary twice hit the existing creator-facts freshness guard. Automatic approval review rejected forcing a creator-facts refresh with the live admin credential because it introduced an additional production write. That refresh did not execute. A materially safer read-only canary passed after the normal updater refreshed the cache. Do not bypass the freshness guard or retry the rejected write as a workaround.

## Remaining work: do not call this fully accepted

1. **Actual backfill and throughput.** Last completed snapshot: 68,364 discovered fans, 68,358 queued, 281 processed, 68,083 pending and six deferred fan profiles; oldest queue roughly 56 minutes. Counts grow as import continues. Every creator still reported incomplete provider history. A processed silent profile is not proof of discovered personal facts. At that snapshot there were only 17 materialized memory rows on Nicky and four on Maile. Two-hour scheduling is not a proven fleet completion SLA. Investigate throughput and finish all available history; do not report queued rows as completed profiles.

2. **Creator drafts.** Nicky's creator job reported processed. Naomi and Maile creator jobs had profile-analysis-deferred errors and retry. Inspect both model failure and publish_creator/PersonalityWorkflow paths. A processed creator queue row may have skipped automatic publication because an existing human review is protected; verify the actual reviewable draft. Alva and Katie creator/text jobs report model-credential-missing. Their saved model selections also need owner review. Diagnose through existing account configuration; no key fallback or copied credential.

3. **Historical versions.** _persist retains source observations in of_profile_observations but compacts history arrays in of_profile_memories. Current /profile/history paginates materialized memory rows, not all old observation versions. Expired distinct events are topic-retrievable, but deeper superseded job/location/value retrieval can lose old values after evidence compaction. Implement topic-scoped observation retrieval and useful historical pagination with evidence validation, statuses, manual-clear/protection priority and binding/tenant isolation. Keep the hard context budget. This is not already finished.

4. **Date forms and extraction quality.** Exact ISO birth-date evidence is supported. Strict value-in-quote validation means natural-language DOB formats need deterministic parsing/evidence handling. Unsupported/ambiguous forms must remain uncertain, not invented. The relative-date parser is deliberately conservative. Add meaningful tests and bounded authorised quality checks.

5. **Visual acceptance.** A dedicated structured-memory editor has not been added. Existing generic inbox facts carry the summary/status/coverage; correction and history APIs exist. Complete the signed-in browser check and any UI work needed. Never invent a passed visual test.

6. **Repository/pipeline reconciliation.** Keep newer runtime fixes. Read current instructions and establish current images/source/schema before more changes. The dashboard remains f656663 at the last check; do not substitute an older dashboard release or merge an old branch wholesale.

Profile APIs use existing account configuration-purpose authentication under:
- /history-memory/profiles/progress
- /history-memory/fans/{id}/profile
- /history-memory/fans/{id}/profile/history
- /history-memory/fans/{id}/profile/memories/{key}/corrections

## Last in-flight diagnostic handoff

Immediately before Jesse stopped work, two read-only SSM inspections were submitted. Their results were not fetched after the stop instruction:
- 39206ecd-35d9-4ca2-afbe-762447733927: current coverage/runtime verification.
- 9954564d-1d45-45bc-9e17-601c3b8529f7: creator queue metadata and PersonalityWorkflow.get diagnostics for Naomi/Maile/Nicky, without printing credentials or message bodies.

The second inspection reports exception type and traceback function/line metadata if draft reads fail. It may help resolve the deferred creator jobs. Do not assume either inspection changed production state; both were read-only.

An authorised laptop working directory exists at C:\\Users\\conta\\codex-work\\profiles-memory-20261029; its current-api directory is the tested integrated API checkout. The shared cloud working tree contains an older base for some existing files plus separate integration snapshots. GitHub's verified candidate and runtime source guards are authoritative. Avoid indiscriminate uploads of workspace files or private runtime/test material.

## Files to start with

- service/onlyfans_runtime/profile_learning/{evidence,temporal,storage,worker,model,manual}.py
- service/tests/test_profile_learning_{evidence,temporal,queue,pg}.py
- deploy/profile-learning/{schema,notifications-postgresql,upgrade-v1-postgresql}.sql
- deploy/profile-learning/runtime-overlays.json
- deploy/profile-learning/{rollout,upgrade-current-runtime,validate_history}.py
- deploy/profile-learning/README.md

No further development, server mutation, retry, deployment or model call was performed by GPT after the stop request. Only GitHub verification and this handoff/package record were completed.
