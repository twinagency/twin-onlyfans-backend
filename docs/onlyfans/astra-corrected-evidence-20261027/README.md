# Corrected Twin CRM candidate for Astra review

Runtime/source pair: backend 0c4d79aa9f736102aa26bb41e4f7561ab1fff248 and dashboard
444fd1303858e6d87204efe5811278657571f8f6.
Operational verification supplement: the codex/twin-crm-astra-repairs-20261027
branch, with image orchestration at 6c3a5a9ac66b2c72f63205ec9f864bb714a39ad8,
image rehearsal at 727ff8f2694ce9bbeebe344581ee3098f7cc8105, and provider
preparation at 2ad2af92a23a9f2ee776e5ba70232fadf5e22db3.
The supplement changes test orchestration, proof and preparation tooling;
the tested application runtime remains the pinned pair above.

## Requested corrections

| Requirement | Completed implementation and verification |
| --- | --- |
| FASTT campaign dispatch | Existing preparation, runner, TextAuthority and single-attempt adapter now accept campaign origin. Manual priority has bounded campaign turns. Automated history, account/fan scope, late takeover/pause/cancel, matching receipts and uncertain holds remain enforced. Actual-boundary synthetic campaign and control regressions passed. |
| Onboarding and proxy recovery | Existing broker owns distinct management-authenticated idempotent reclaim, exact reservation identity, audit evidence and shared assignment/browser fences. Used, enrolled, bound, ambiguous and saved-network references refuse. Standalone creator first sign-in reuses server preparation with sticky pending ownership. Saved-network reconnect stays on its saved network. |
| Actual sender reconciliation | Deployed ai_chatter and native engine overlay functions match candidate ASTs and are preserved. The deployed extra 0092 schema declaration is preserved; schema head/ranking is intentionally superseded by the additive 0093–0096 candidate chain. Deployed post-purchase AutomationRun attempt protection remains alongside the new offer fence. Latest 08:44 UTC hashes/config/image match the reconciled baseline, with no changes. |
| Release and rollback | Six immutable backend/sign-in/dashboard forward and rollback packages built successfully. The actual retained rollback Docker image passed the existing populated 0096 backup/restore and rollback assertions: 2 passed, 0 failures/errors/skips. Earlier default legacy-HMAC hard-cutover and logout-action rehearsals passed, including replay denial and unrelated-session retention. Required production maintenance order is retained in [the release gates](../astra-correction-release-gates.md). |
| Controlled provider/model preparation | Exact proposed account, recipient, fixed network, model, persona hash, item/media/previews and US$3.00 price are recorded. Offline model route is compatible; deployment network alias is verified. Manual/API, one-call no-fan-transport model-draft and bounded one-reply CLI preparation are recorded. No provider/model send was performed. |

## Final acceptance gate

PASS: all six acceptance shards and the hosted aggregate in run 40473098317.
The aggregate accepted 2,260 cases: zero failures, errors, skips or
deselections, with complete non-overlapping collection and the two required
isolated scripts passing. See aggregate-summary.json and acceptance/ for exact
commands, manifests, node identities, JUnit outcomes and logs.
Both native suites passed on the pinned backend: 28 SQLite commands and
7 PostgreSQL commands, each with successful exit and no reported skipped cases.
The first full run exposed a test fixture still mocking the old manual-only
dispatch function. Its exact assertions were retained while the fixture was
updated to the operator dispatch boundary and explicit empty-campaign setup.
The successful native rerun uses that corrected fixture.
The Windows local script attempts exited 15 and are not claimed as passing;
the Linux hosted evidence is the required result.

Old 2,182-test evidence belongs only to backend d6811b124928f8167e6625ce9db5db81bf227fad
and dashboard 871b3e8955bf8af357f6f66d56c4446bf98fee8e. It was not reassigned.

## Immutable artifacts and roles

See immutable-images-summary.json for Docker image IDs, archive SHA256 values,
immutable GitHub artifact identities/digests and expiration timestamps.
API, worker, sender and proxy broker use the same backend image bytes with the
existing role entrypoints; they are not separately rebuilt versions:
- API: uvicorn service.onlyfans_app:app --host 0.0.0.0 --port 8787 --workers 1
- Worker: python -m service.onlyfans_worker
- Sender: python -m native_pilot.supervisor, workdir /app/service
- Proxy broker: python -m managed_proxy.api, workdir /app/service
- Sign-in: xvfb-run -a node creator-sign-in/server.mjs
- Dashboard: node server.js

Backend dependency freeze and image configuration are in the full image
manifest. Sign-in and dashboard retain their committed lockfiles. The deployed
environment/config fingerprints are evidence, not permission to copy secrets.
The existing backend compose definition still needs an explicit immutable image
override at deployment; do not use a floating tag or rebuild during release.
Retain/download the named packages before their 25 January 2027 artifact expiry,
and retain the immutable package digests in the final deployment record.

## Remaining release blockers

- Astra final review and an approved private execution/production maintenance window.
- Fresh verified connection and authenticated management actor/revisions; confirmed
  control of recipient 318022380 and explicit bounded model/outbound approval.
- Actual approved-window CRM+broker backup and dashboard session-store recovery IDs,
  target identity, closed admission, drained/fenced grants, stopped writers and
  disabled restart/late-browser completion. No production maintenance was performed.
- Required order: immutable artifacts; admission/writer quiescence; verified
  restorable backups; bounded statistics preflight; explicit 0093–0096 upgrade/check;
  explicit additive broker recovery upgrade; additive dashboard session SQL before
  dashboard code; pinned roles with outgoing automation disabled; controlled QA.
  On statistics/migration failure stop and inspect rollback. Do not raise the
  5-second lock/30-second statement limits or hand-stamp a migration.
- Provision a distinct private broker management token through the existing secret
  owner during the approved window. No management credential is exposed to creators.

Default legacy-session policy is hard cutover, with
DASHBOARD_ALLOW_LEGACY_ADMIN_SESSIONS unset/disabled. Existing legacy master
cookies reauthenticate; server-backed owner and agency sessions remain.
Current-token logout replay is denied. Communicate the re-login requirement
before the approved release, without enabling temporary legacy compatibility.

The serial NativeEngine/global hook guard remains. Neither 120-second p95 nor
250-creator capacity is proved; no scaling rewrite is included. Flirt AUTH-02
and AUTH-03 remain jointly deferred and must not be enabled independently.
No production deployment/migration, allowance reset, fan-automation expansion,
or saved model/persona change was performed.

## Review navigation

- [All 36 requirement dispositions](REQUIREMENTS.md)
- [Controlled provider and model packet](../astra-controlled-provider-verification.md)
- [Release order, rollback and legacy-session policy](../astra-correction-release-gates.md)
- [Exact image and artifact identities](immutable-images-summary.json)
- [Full exact-pair acceptance run](https://github.com/twinagency/twin-onlyfans-backend/actions/runs/40473098317)
- [Six immutable artifact builds](https://github.com/twinagency/twin-onlyfans-backend/actions/runs/40473190809)
- [Actual-image populated rollback rehearsal](https://github.com/twinagency/twin-onlyfans-backend/actions/runs/40474649744)

The additional focused XML files are intermediate repair regressions, not extra
cases added to the final aggregate. The final aggregate alone owns the corrected
pair count. Session logs are tied to dashboard 444fd1303858e6d87204efe5811278657571f8f6.
