# Code handover: 28 October 2026

## Update after Ronan's 10:51 AEST launch report

The earlier deployed-baseline table below records the pre-launch inspection and is historical. Ronan now reports these versions live:

| Component | Reported source | Reported image prefix |
| --- | --- | --- |
| Backend API / worker / sender | `7f4eaf0bcb0b461300a0301330acd773f2b3d8f0` | `bc2a3dc7` |
| Twin dashboard | `2d0653343afddca830a9734dcc87e5eef4b2cde4` | `d5d4cc71` |
| FLIRT dashboard | `2e5184d2e8ad0f5582002142f9f98785108ffff5` | `9305291f` |

He reports migration `0098_ledger_exclusion` applied, sender scope `db:*`, Nicky/Naomi enabled for four chats each, and Maile/Katie Price/Alva Jay held at account level. These runtime facts and the provider receipt are operator-reported; this follow-up did not independently inspect production or send messages.

GitHub was independently checked:
- All three source branch heads matched the reported full commits.
- Backend `codex/onlyfans-of-b1` and Twin `release/twin-20261028` advanced concurrently during review and exactly matched the respective reported commits before this documentation update.
- FLIRT source was merged into `release/flirt-20261028` via [PR #308](https://github.com/twinagency/twin-dashboard/pull/308), merge `b1d05f627b614a19289370e03a6f6d46aa5d6e85`.
- FLIRT exact-head verify check passed. Twin exact-head runtime packaging, its four OnlyFans checks, production build and creator ownership check passed. The broader [CI job](https://github.com/twinagency/twin-dashboard/actions/runs/40610022761/job/122559030069) failed its login smoke test: `/admin-auth` returned 500 rather than 303 with "The session service is unavailable." The consolidation changes do not touch login/session code; this is still an unresolved validation failure.
- No backend GitHub check runs were present on the reported head. Additional local pytest collection failed because the available environment lacked HTTP client dependencies and the supervisor test hit a Windows SQLite file lock. Those attempts are not passing test evidence.

The output guard is narrower than the email's claim that every mention of AI/bot/clone is blocked: `native_pilot/identity.py` checks specific self-description patterns. Its tests deliberately allow ordinary references such as "i love ai art tho". The prompt also instructs the model never to claim to be human; the inspected guard is not a general proof that all such claims are mechanically blocked.

Still awaiting operator evidence/decisions: takeover -> manual reply -> hand-back -> resumed AI, capacity measurements, final dated activation record, handling Maile's 610 legacy automated chats, and ownership of identity-question flags. New-creator connection/import and fresh inbox synchronization also need explicit end-to-end proof if not covered elsewhere.

This follow-up only consolidated source and updated documentation. It did not deploy, run a migration, change account/chat controls or alter Ronan's live work.

## Branches for new changes

| Repository / product | Raise changes against |
| --- | --- |
| twinagency/twin-dashboard: Twin and Moxy | `release/twin-20261028` |
| twinagency/twin-dashboard: FLIRT | `release/flirt-20261028` |
| twinagency/twin-onlyfans-backend | `codex/onlyfans-of-b1` (existing default branch) |

The dashboard has two separate product lines with different code and configuration. Do not merge FLIRT wholesale into Twin/Moxy. Dashboard `main` still has an automatic production deployment workflow; this handover used named release branches instead. No deployment, restart, schema change, account activation, payment test or scope expansion was performed.

## Deployed baseline and prepared additions

Read-only container inspection during this handover confirmed:

| Role | Deployed source | Image |
| --- | --- | --- |
| Twin/Moxy dashboard | `4599999761dbf2ff2b517359642d10262324867a` | `sha256:fccf205a34fe002c6344062305128f6b302a2e1e0c5d92caf7beac30d2a79c95` |
| FLIRT dashboard | `a926800fecdf611c4ef49d2d413be4ef647c0d86` | `sha256:fe31b7c80dbce7162aed2e920e91e67ec36eb40d4fdfcd521497330f66a4fb37` |
| OnlyFans API, worker, supervisor and broker | `293b23dbdf8dd5e97f27d6c0bef8ef4710415c06` | `sha256:b4c3ec13342111c1eadc75ad224ab314b659ff60494d78513b37dbdeb67e33ec` |
| Creator sign-in | See committed-source proof below | `sha256:b63d926bf03c7b6abe5a52c9bfa77b4d6821d7543d16ec1ab64155c1dc925b6e` |

The release branches contain those deployed baselines plus prepared fixes:
- Twin/Moxy PR #288: reply-box takeover and explicit refusal feedback, merged at `e713f7bf0bfc05977e2e9f5e8b3f2d55d46a27c4`.
- FLIRT PR #304: AI ownership badges, scoped AI-only filtering and persisted handover markers, merged at `424eb0d14308888d6c86cc97d375981a5408baf7`.
- Backend PR #31, consolidated through #37: fail-closed Smart List validation, merged at `6ca3101c3bdd431892c573ccaf65710d40c739f2`.

**These additional fixes are merged source, not a claim that they are deployed.** The live base commits remain separately recorded above.

## Requested PR dispositions

| Dashboard PR | Disposition |
| --- | --- |
| #306 | Merged into release/twin-20261028 |
| #307 | Merged into release/flirt-20261028 |
| #304 | Merged into release/flirt-20261028 after focused verification |
| #288 | Merged into release/twin-20261028 after focused verification |
| #301, #299 | Closed; source already included in the FLIRT deployed baseline |
| #300, #298, #289 | Closed; source already included in the Twin deployed baseline |
| #295, #294, #293 | Closed; patch-equivalent changes already included in the Twin baseline |

Backend #36 consolidated runtime `293b23d`, compatible controls, evidence `d17fa2c`, and recovered operations scripts into the existing default branch. Backend #33, #32, #30, #29, #28, #27, #25, #22, #21 and #19 were closed after confirming their heads were ancestors of that consolidated source.

Older proposals were closed with an explicit archival note and their exact head SHA in each PR. Their branches were retained; closure does not assert that every proposed change is incorporated or deployed.
- Dashboard: #273, #259, #255, #253, #251, #195, #193, #178, #177, #170, #153, #78, #30, #28, #26, #24, #8.
- Backend: #26, #24, #20, #6.

In particular, experimental capacity work is not production capacity proof. Any still-needed differences should be reapplied as focused PRs against the branches above.

## Operational source and evidence

Backend `docs/onlyfans/astra-compatible-release-20261028/operations/` includes the preserved backup, restore, cutover, scoped activation and verification scripts.
- Added the local `astra-current-financial-gates.py`.
- Added the local `caption-ssm-exec.cjs`.
- Verified the local `astra-post-deploy-check.py` matches the already committed version after line-ending normalization.
- The local compatible-release and deployment-record worktrees matched GitHub at `293b23d` and `d17fa2c`, with no tracked modifications.

The SSM helper uses the existing AWS CLI `tsocial` profile, `ap-southeast-2`, and instance `i-09a9dd3bd749643a5`. It accepts a Python script path or `get <command-id>`. Its adjacent execution outputs must remain outside Git. The dated operation scripts are not an unattended deployment command. Read the release README and scoped-activation record before executing them.

Creator sign-in source is committed under `creator-sign-in/`, with `Dockerfile.sign-in` and `deploy/creator-sign-in-compose.yml`. The prior release evidence verified its server file against `293b23d:creator-sign-in/server.mjs`, SHA256 `2678c63ef05abaea2d4471e70ae24b04dcebaafbc624ffd89d0801339b77452f`. The image carries the earlier revision label `1c2030e`; use the source/hash evidence rather than treating that label as complete provenance. Rebuilding now is not guaranteed to reproduce the historical image digest.

## Verification in this handover

- Twin: control transport/acknowledgement checks, real browser takeover/refusal/conflict/in-flight hold regressions, and TypeScript check passed on the merged code tree.
- FLIRT: AI visibility/scoping/pagination/handover regressions, 115 read-boundary checks, creator onboarding choice/redirect/access checks, and TypeScript check passed on the merged code tree. An initial onboarding test invocation lacked the TS loader; rerunning with the required `--import tsx` passed.
- Backend Smart List change: 34 focused synthetic cases passed locally. No provider or model calls were made.
- Required Python operation scripts parsed successfully and the SSM helper passed Node syntax validation.
- Existing broader release acceptance evidence remains attributed to its original exact source pair. No new full production acceptance or capacity claim is made.

## Local preservation completed

The approved source archive is pushed and verified on GitHub:
- Dashboard: 73 snapshots at [archive/local-work-20261028](https://github.com/twinagency/twin-dashboard/tree/archive/local-work-20261028/docs/handover-local-20261028), commit `d27a0fce51ad25dc740d30c0be633405e4dde5f4`.
- Backend: 110 snapshots at [archive/local-work-20261028](https://github.com/twinagency/twin-onlyfans-backend/tree/archive/local-work-20261028/docs/handover-local-20261028), commit `d2b5185014b569f0238a8ca217ee8ce19e7ca84f`.
- Older dashboard hybrid CRM branch: [archive/local-hybrid-crm-foundation-20261028](https://github.com/twinagency/twin-dashboard/tree/archive/local-hybrid-crm-foundation-20261028), commit `797a6ece874728effc14daabca3ae5940624bc6a`.
- Older dashboard creator parity branch: [archive/local-tether-creator-parity-20261028](https://github.com/twinagency/twin-dashboard/tree/archive/local-tether-creator-parity-20261028), commit `e38004c88f8190ed828fb9bd89ef6b1a73e2e349`.

All 183 snapshot blobs passed SHA256 comparison against the prepared manifests before commit. GitHub branch refs match the published commit hashes, and the remote manifests contain the expected 73 and 110 entries. Archive worktrees were clean after publishing. Original user worktrees remain intact. Snapshots use a `.source.txt` suffix to preserve original bytes without activating unfinished code. Review before applying anything from these archives to a release branch.

## Repository ownership and access

All five repositories identified in this handover are private and owned by the `twinagency` organization. GitHub confirms `ronan-com` has admin permission on each:
- [twin-dashboard](https://github.com/twinagency/twin-dashboard)
- [twin-onlyfans-backend](https://github.com/twinagency/twin-onlyfans-backend)
- [twin-bot](https://github.com/twinagency/twin-bot)
- [twin-sexbot](https://github.com/twinagency/twin-sexbot)
- [twin-userbot](https://github.com/twinagency/twin-userbot)

Ownership and permissions for the older three were verified through the laptop's authenticated GitHub CLI because the connected GitHub app exposed only the first two. No access changes were necessary. Open the links while signed into `ronan-com`; the supplied 404 screenshot showed GitHub signed out.

This handover consolidates the two repositories named above. It does not claim every historical repository or every local file from prior sessions has been consolidated.

Full activation, unresolved purchase handling, model budgets, provider reconnection and the live paid journey remain separate handover decisions.
