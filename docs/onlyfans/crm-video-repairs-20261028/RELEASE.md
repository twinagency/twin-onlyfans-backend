# CRM video repairs, 28 October 2026

This is the implementation and release checklist. Local results are separate from production acceptance. The 28 October activation record supersedes the earlier hold; schema 0098 is retained. No account or fan automation settings are broadly enabled.

## Verified baseline

- Production schema: `0098_ledger_exclusion`.
- API and FASTT: source `7f4eaf0bcb0b461300a0301330acd773f2b3d8f0`, image `bc2a3dc7e012d66a3c442b7fe4eff0a76d1bfca4c7660b0f050eb7dfd562830e`.
- Worker: image `c3ef3b6f3932df063f0fd94b5ea2996fc4b3ce02f77318ac4fd885a787b6a21a`. All 718 Python files compared against the API image; only `onlyfans_runtime/recent_inbox.py` differs. Its PR #38 continuation fix is preserved.
- Dashboard advanced during this work to `38d6ac20d33403339a635c9668e5ef0189573ea1`, image `fbe7ea1c86329d1fece091eee14300fb166b9a693001ef3f3bc30e6ce81c06fb`. Its sync-warning and connection-link changes are incorporated.
- Nicky and Naomi enabled, sender heartbeats running. Maile, Katie Price and Alva Jay remain runtime-disabled.
- Manual failure reproduced through the browser. Expected/current revision `7/7`, effective human takeover, no unresolved attempt, account and policy enabled, allowance `200/500`, queue empty. Authority reason `history_quarantined`; no test intent was created.
- Verified quarantine: Nicky 141, including 140 on the designated test fan. Maile 1,007. All text differences are equivalent provider HTML. Four Nicky records also have missing legacy message media arrays; their exact ordered media IDs exist in account/fan/message-scoped `vault_sends`.

## Checklist

| Issue | Reproduced | Implementation | Local verification | Deployed/live |
| --- | --- | --- | --- | --- |
| Manual reply after takeover | Browser and authority | Representation-only intake matching and audited idempotent recovery; raw observations, message IDs, budgets and all send guards retained | 84 PostgreSQL regression tests passed; verified repair and replay checks passed | Deployed; two provider-confirmed manual messages, one intent each, live idempotency replay passed |
| Specific refusal reasons | Source and browser | Allowlisted codes end to end, revision/hold diagnostics, composer feedback | Control transport and refusal tests passed | Deployed; live stale revision returned 409 with exact reason and revisions |
| Effective AI status | Runtime flags and supervisor heartbeat verified | Saved fan setting separated from account, connection, sender freshness and holds | AI visibility and control tests passed | Deployed; Nicky ready, Maile account pause visible; fresh-input AI phases pending |
| Delivery states and resolution | Ledger/projection inspected | In-flight, accepted pending projection, delivered, failed and unknown are distinct; exact receipt scope; positive-only audited resolution with server-side provider verification | Ledger, recovery and HTTP/PostgreSQL checks passed; API-to-worker regression passed | Deployed; 13 worker/API/evidence regressions passed; live worker capability confirmed; genuine unknown retained |
| List/thread/control consistency | Recording and source | Refresh thread on new provider identity/activity; preserve loaded history, drafts and uncertain command identity; reject late sequences | Inbox refresh and selection checks passed | Deployed; browser draft/account isolation, 200 loaded messages and synchronized new send verified |
| Server search | Recording and source | Bound/account-scoped name, username and numeric search; copied handle normalization; query-bound cursors and pagination | Normalization and transport checks passed | Deployed; browser copied-handle search and all four live API searches passed |
| Sync/loading | Browser and source | Separate connection, intake, history, financial and sender observations; attention counts; preserve background content | Sync warning and empty/loading checks passed | Deployed; live health read passed; incomplete history/financial coverage remains explicit |
| Presentation/data | Browser and source | Provider profile display fields, safe fan avatars, reconciled spend with coverage, separate attention reasons, recorded authorship, Brisbane display time, action-specific pending feedback | UI and time checks passed | Deployed; all five creator profiles observed; recorded authorship, partial spend and AEST verified |

Local evidence: 101 preview checks, 22 focused Python tests plus 10 subtests, read-service script, control transport, AI visibility, sync warning, UI, inbox refresh and TypeScript checks. These are synthetic/local checks, not native OnlyFans delivery proof.

## Recovery and release

1. Recheck current container IDs and source hashes before any swap. Abort on unreviewed drift. Preserve the dirty laptop/server checkouts.
2. Use only the disposable PostgreSQL test container and internal test network for regression data. No production credentials or provider/model network access enter those tests.
3. Build code overlays over the verified running dependency images. Preserve the newer worker continuation change. No schema migration is required.
4. For history recovery, call `history_repair.inspect` for one authorized account/fan, then `repair` with its exact fingerprint, a stable command key, an authenticated/internal import actor and a reason. Repeat the same key safely. Changed evidence refuses. Only equivalent text and exact retained pack identities qualify; substantive differences remain quarantined.
5. Recheck account and policy state. Do not clear intentional pauses, ownership, unknown delivery holds or allowance counters.
6. Live text journey is limited to the designated Nicky test conversation. Verify native OnlyFans/provider IDs and exactly one durable intent/receipt. Other creators stay unchanged. Unapproved purchases and arbitrary fan messages are excluded.
7. Delivery resolution accepts positive evidence only. Missing history never proves failure. Enable its capability only after the scoped endpoint and original recovery authorities pass their tests.

## Rollback

Keep stopped image-retention containers and private compose snapshots for the exact pre-swap images. Restore only each changed service image, using the same compose project/configuration and `up -d --no-deps --no-build --pull never`. Keep schema 0098. Do not restore an older database dump over newer receipts, reset send allowances, erase unknown attempts or delete raw intake. Verified history repairs retain immutable audit and source evidence; later corrections require a new audited decision.

Live acceptance, final image/commit identities and remaining blockers must be appended after release. Pending rows must not be described as passed or deployed.


## Live acceptance evidence

These are production observations, separate from isolated tests.

- History recovery repaired Nicky 141 and Maile 1,007 records in 43 audited commands. Each command was replayed with the same key. All 1,148 raw ingress payloads, event keys, binding/fan/message identities were verified unchanged. Remaining quarantine was zero. No account or fan automation was enabled by recovery.
- Authorized manual reply on `of-176774528 / 318022380`: provider message `11829924311011`, intent `fd2332ba-5b07-4b69-b0ae-243c40b13dd3`, attempt `db193a23-bda7-48b9-a204-78117e2836b3`, control revision 7. Exactly one accepted intent, message and delivery row; unresolved pointer cleared. The actual receipt account, fan, attempt, provider identity and body matched. Replaying the original live command twice returned the same intent and kept one message.
- A second browser journey performed hand-back, takeover, manual send and hand-back. Provider message `11830187266684` appeared as a confirmed human message in the CRM. The list preview and open thread advanced together; loaded history grew from 200 to 201 messages.
- A deliberate stale command returned HTTP 409, code `revision_changed`, expected revision 8, current revision 9 and holds `[revision_changed]`, without changing control state.
- Browser search `@u318022380` returned only the selected account's Gervonta row. The live API also verified `u318022380`, `318022380` and `Gervonta`, each in 0.09–0.18 seconds.
- A Nicky draft survived switching to Maile and back. Maile had an empty draft and its own account-prefixed conversation ID. Maile displayed “AI enabled for this fan. Account paused.” No Maile send or account activation was performed.
- Loading older history retained 200 messages and the draft through subsequent refresh and control operations. The composer and list showed the confirmed manual send with Brisbane timestamps, recorded human authorship and no fabricated AI attribution.
- Gervonta's reconciled cleared spend was USD 6.00 with partial historical coverage, not a complete lifetime total. Unknown spend on Maile stayed unknown.
- One unresolved pointer, Maile fan `49192430`, `fastt-pilot-543`, had no matching settlement proof and was retained. No unknown attempt was cleared by guessing failure.

## Verification boundary

Native OnlyFans browser access was rejected by the browser's site-safety policy. No alternate browser, direct-provider script or indirect native-view workaround was used. Native screen confirmation and the two fresh-input AI phases of the full journey remain pending. The request for fresh fan input received no confirmation, so it was not assumed to have happened.

The live CRM journey ran on dashboard `038fb83b`; the final UI source `81b74018841803be2f6356de6c960653c416fdde` passed both CI runs and creator workspace verification. Its promotion completed at 2026-10-28 15:58:44 UTC using the existing deployment credential. Running image and registry digest: `sha256:23724ae8a7edb9fb2850fadaf85e030077fef125422b22f2fdaf45e2c7804096`. Login and static-asset HTTP smoke checks passed. Existing runtime environment, network, port, resource and restart settings were copied from the running container. The server's dirty checkout was not read or modified.

A final review found that delivery evidence must run in a dedicated worker child, because the API role is explicitly prohibited from opening the legacy provider client. Source `f234c641a0afd68b3314ad428284fcc24c96f1c2` queues an immutable audited request; a one-account worker child performs the original scoped positive-evidence reconciliation. A current worker heartbeat is required before the API advertises or accepts the action. The final worker gate passed 13 tests before promotion; the production audit verified the worker capability is operational on Nicky.

Uploads and paid delivery were not diagnosed as broken by the recording and were not exercised with a live purchase. Existing upload/media regressions ran in CI; exact retained media identity matching was exercised in the history repair tests. No purchase or unapproved fan send was performed.

## Release references

- Backend PR: https://github.com/twinagency/twin-onlyfans-backend/pull/42
- Dashboard PR: https://github.com/twinagency/twin-dashboard/pull/313
- Final UI CI: https://github.com/twinagency/twin-dashboard/actions/runs/40706579597
- Final UI PR CI: https://github.com/twinagency/twin-dashboard/actions/runs/40706590424
- Creator/browser verification: https://github.com/twinagency/twin-dashboard/actions/runs/40706590452
- Dashboard promotion: https://github.com/twinagency/twin-dashboard/actions/runs/40720734105
- Worker evidence release gate: https://github.com/twinagency/twin-dashboard/actions/runs/40723022560

## Concrete rollback

Dashboard: the immediate previous working container is stopped as `next-app-crm-rollback-038fb83b`; the pre-repair dashboard remains `next-app-crm-rollback-4686e2e0`. After checking that `next-app` still runs the source being rolled back, stop it, rename it to a unique retained name, rename the chosen rollback container to `next-app`, and start it. Do not remove rollback containers or run a global image prune.

Backend: private snapshots are under `/var/lib/twin-release/crm-video-repairs-20261028/`. The original `compose.before.json` retains API/FASTT `bc2a3dc7` and the worker's `c3ef3b6f` image with its inbox continuation fix. Restore only the desired service image entries and the prior resolution feature flags into the current `/etc/twin-onlyfans/media-agency-release-compose.json`, then use `docker compose -p twin-onlyfans -f /etc/twin-onlyfans/media-agency-release-compose.json up -d --no-deps --no-build --pull never api worker fastt-supervisor`. Preserve any newer unrelated configuration. Keep schema 0098 and all receipts, raw ingress, audit records, idempotency keys, allowance counters and genuinely unknown attempts.


Offline evidence details: the standalone pilot ledger script completed 300 checks and the reporting script completed 26 checks. The final UI source passed all three recorded CI/creator workspace runs. A new worker-role test explicitly rejects provider execution in the API, exercises durable enqueue and replay, and checks that a changed revision retains the unknown attempt.


## Final deployed versions and acceptance

Verified at **2026-10-28 16:13:05 UTC**. All four containers were running with zero restarts.

| Component | Source commit | Running image |
| --- | --- | --- |
| Dashboard `next-app` | `81b74018841803be2f6356de6c960653c416fdde` | `sha256:23724ae8a7edb9fb2850fadaf85e030077fef125422b22f2fdaf45e2c7804096` |
| Control/read API | `f234c641a0afd68b3314ad428284fcc24c96f1c2` | `sha256:80341b333fb171478d555a5d729fb6375420afaf5d0a38b64ad4cacc467bf756` |
| Inbox/manual worker | `f234c641a0afd68b3314ad428284fcc24c96f1c2` | same `80341b33…` image |
| FASTT supervisor/sender | `f234c641a0afd68b3314ad428284fcc24c96f1c2` | same `80341b33…` image |
| Database schema | `0098_ledger_exclusion` | unchanged |

The final read-only audit passed: https://github.com/twinagency/twin-dashboard/actions/runs/40723707977

- Both provider IDs `11829924311011` and `11830187266684` had exactly one accepted intent and one message row. CRM read-back reported both as `sent` with `human` origin, provider times `2026-10-28T14:07:04Z` and `2026-10-28T14:31:06Z`.
- The test fan is `automated`, takeover `none`, revision **10**, after successful hand-back. No old incoming message was replayed to manufacture an AI reply.
- Nicky and Naomi remain enabled with running sender heartbeats. Maile, Katie Price and Alva Jay remain disabled with held senders. The hard default-off policy for new conversations remains locked.
- Quarantine is zero. Maile fan `49192430`, attempt `fastt-pilot-543`, remains unresolved. The repair neither cleared it nor inferred non-delivery.
- The provider-evidence action is exposed only when the current account has a fresh supporting worker heartbeat. Nicky passed this check. Naomi's evidence action was conservatively `not_configured` in this audit despite its separate AI sender being running.
- All five accounts had a stored creator-profile observation. Provider images still use a safe fallback when an avatar is absent or fails to load.
- Nicky's fan spend remained **USD 6.00, partial coverage**, with financial observation `2026-10-28T14:04:13Z`. History and financial import work is still incomplete and remains visible in Needs attention.

Worker release: https://github.com/twinagency/twin-dashboard/actions/runs/40723022560 . Its isolated gate completed **13 passed** in 50.49 seconds, covering API-only enqueue, worker-only execution, exact positive evidence, replay, revision races, existing send-recovery guards, scoped SQL search and profile observations. SSM release command: `ef83a031-b4c5-4e5a-9659-f37fcf205520`. The database and test credentials were disposable and isolated from production/provider networks.

The worker-resolution rollback snapshot is `/var/lib/twin-release/crm-video-repairs-20261028/worker-resolution-f234c641/compose.before.json`. It restores the immediately preceding `1d5e4a31` / `afe0d86c` services and prior worker feature flag. A stopped `crm-resolution-rollback-f234c641` container retains that image. Reverting this particular worker patch must also switch off the API delivery-resolution capability; the old API implementation cannot execute provider reads under its role guard.

Outstanding live acceptance is limited to fresh fan input followed by AI replies and native OnlyFans screen confirmation. Upload/paid delivery was not live-tested. No approval was requested for already-authorized deployments, and no account/fan pause, message budget, unknown attempt or raw history was reset.
