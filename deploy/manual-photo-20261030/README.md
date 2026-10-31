# Manual free-photo history hold repair
30 October 2026. Feature commit: 164b0d6ca5dabf5abcb3da7e994fa52df9e932c5. PR #62.

## Result
Deployed to the API, FASTT supervisor and legacy worker on twin-dashboard-prod (i-09a9dd3bd749643a5, ap-southeast-2) using the existing jesse-gpt profile, following explicit user authorization.

An explicitly selected free vault attachment under human control no longer requires archive recovery or account-wide quarantine clearance. Paid manual offers, text-only replies and automated/campaign/engagement origins retain their history gates. No history rows were cleared. No model, pricing, budget, cap, activation, takeover, connection or purchase settings were changed.

At 2026-10-30T10:18:50Z, a read-only production check of Maile (of-323358988), Gervonta (318022380) found one retained quarantined history row, delivery and media enabled, and no archive refusal under effective operator-control evaluation. The real chat was automated with no takeover; its actual free-manual evaluation correctly returned human_control_required. The operator must take over before sending. The operator-control evaluation used a detached state copy; no actual takeover was performed.

No customer photo/message was sent by this release. Live provider acceptance of a manually selected photo remains unverified.

## Validation
- Release workflow 41034367294: all five PostgreSQL/manual/ledger/upgrade/native/contextual groups passed.
- Manual attachment/API group: 52 passed, including 17 new archive-boundary/control/exclusion cases.
- All three exact production-derived images passed 21 offline guard checks each before and after cutover (63 distinct image/check combinations).
- API healthz and authenticated schema readiness returned HTTP 200. Worker healthy, API and supervisor running; zero container restarts at final cutover verification.
- Runtime environment values, command, entrypoint, user, working directory, health/stop settings, resource limits, restart/security settings, ports and mounts matched captured predecessors.
- Subsequent read-only image and target-fan guard verification passed.

## Source and deployment reconciliation
Production is a set of existing overlays, not an exact whole-branch checkout. The API's existing manual=True paid-media checks were retained. Only the six archive-exception edit blocks were applied to the exact current text_delivery.py of each service. No whole repository tree was deployed.

A concurrent manifest update was caught before any service replacement. The only change was fastt-supervisor.image being reconciled from the stale 2fa1dd image to the already-running 683934 image. It was reviewed and preserved. Environment list order changed during recreation; comparison was corrected to unique key/value semantics, preserving all actual values. The original snapshot and reconciled snapshot are both retained.

| Service | Captured predecessor | Deployed image | Deployed text_delivery.py SHA-256 |
| --- | --- | --- | --- |
| api | 17ff96b2f9e352f5daad108b66caebd932af0c1092a2f8c41eec7a52054e26ca | 76b172a539e1a44998ffd1122348d36c19fb55544a1ce49044972a94d4747ccd | 0a3cb3bb716a0408e2af448d87e0c2d6e776082118da7bc66261d3a947d93c41 |
| fastt-supervisor | 6839345480127fdb28e4a958a0ffeb80e19dcc0ff6a1925b9780787912968645 | bff962697d6e986ba4a7c04088c0676b18690dec477d43ba3aaab0d53f08a050 | a68fd6bc6d8d1b0b782e8c0c7cb24fba9753cf7802d662d6c14d7b6a8f244848 |
| worker | ce1bcd5eeade5381890e633c3127cd0787b48553f43e330a6925e02ab84545e5 | 1a04a11e11827f1490b7987dfe188122e6b9f5857ec64c91eabf14a4adc8f777 | a68fd6bc6d8d1b0b782e8c0c7cb24fba9753cf7802d662d6c14d7b6a8f244848 |

All image digests in the table use the sha256 prefix.

## Operations and rollback
Host evidence: /var/lib/twin-release/manual-photo-20261030-164b0d6. Restricted inspection/configuration snapshots remain only on that host and are not included here. The directory retains source before/after files, guarded diffs, build/smoke/cutover logs, prepared.json, deployed.json, health.jsonl and live-guard.jsonl.

Executed SSM preparation: b1fa0426-44c0-4c35-b513-368d900402f1.
Guarded no-change stops: 0b332326-9102-4d4d-a563-ac45826a9f72 (concurrent manifest change), 2524c14b-5566-4ef5-8b49-e3e83398deeb (environment list ordering).
Successful cutover: ca6f74a3-888d-4edf-a361-435a6a2b2f74.
Final live verification: b249049f-c4ea-4b63-978c-c29d5dd5802d.

Rollback must inspect current identities and preserve any newer work. Replace only the affected service image in the CURRENT /etc/twin-onlyfans/media-agency-release-compose.json with its captured predecessor, preserving its captured live command and all unrelated fields; run docker compose -p twin-onlyfans -f /etc/twin-onlyfans/media-agency-release-compose.json up -d --no-deps --timeout 180 SERVICE. Do not restore an older whole manifest or sender image. No schema rollback is needed. Restoring an older image restores the history hold.

Bundled scripts record this one guarded release and its exact baselines. They are not an unguarded deployment command for a later production state. prepare.py has exclusive artifact creation and cutover.py requires the reconciled snapshot; do not rerun without reviewing current images/source/manifest and choosing a new artifact directory.
