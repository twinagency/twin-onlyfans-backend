# Manual paid photos: deployed 30 October 2026

Source: `7b6d9fbdfb0819c768704b91b3b4342c114d9d2a`. PR #64 is stacked on the free-photo repair in PR #62.

## Behavior

An operator-selected vault attachment, free or priced, bypasses unrelated transcript archive freshness and quarantine holds at prepare, admission and immediately before transmission. The exception comes from the normalized selection at prepare and the immutable saved manual-vault snapshot thereafter; valid price and matching frozen terms remain required.

Paid attachments still check this recipient's unresolved payment evidence and known media ownership at all three boundaries. API already used the deployed PR #53 manual-commerce helper; worker and supervisor were still calling the broader account-wide helper. This release aligns those two calls without changing the helper or ledger implementation in production.

Takeover/staff ownership, mute/no-contact, revision and media provenance checks, exact price/previews, caps, one-use transmission and unknown-send holds remain enforced. Text-only and automated/campaign/engagement sends retain archive checks. No schema, activation, pricing settings, budgets, allowances or purchase records changed.

## Validation

- API application candidate: 69 PostgreSQL tests passed in 198.74 seconds.
- Sender application candidate: the same 69 tests passed in 196.80 seconds.
- Includes 34 archive/control/purchase regressions, 20 vault-composer cases and 15 manual API cases. Paid ownership/payment holds arriving before prepare, while queued and after admission all block before transmission.
- Exact candidate application trees were copied into the existing compatible test-dependency images. The disposable PostgreSQL 16 service ran on a dedicated internal Docker network, with resource bounds and no production credentials or mounted production data. It and its network were removed after success.
- Each of the three production-derived candidate images passed 27 offline guard checks before and after rollout.
- API health and authenticated schema readiness returned HTTP 200. Worker healthy; API and supervisor running; all three zero restarts.
- GitHub Actions runs 41042532029 and 41042922900 ended with startup_failure before any jobs. Those are not reported as passing CI. The isolated candidate-image PostgreSQL runs above supplied the release verification.

## Live result and limits

At 2026-10-30T10:59:54.038087Z (20:59:54 Brisbane), read-only Maile/Gervonta verification found:

- One quarantined archive row retained.
- No archive refusal when evaluating effective operator control.
- No recipient-specific manual-commerce hold.
- Delivery/media enabled, 500 of 2000 units used, no queued or active intent.
- Actual conversation mode remained automated/none, correctly requiring Take over.

The operator-control evaluation used a detached object; it did not change the conversation. No photo/message was sent, and actual provider acceptance of a user-selected paid photo remains unverified.

## Exact images

| Service | Predecessor | Deployed | Deployed text_delivery SHA-256 |
| --- | --- | --- | --- |
| API | `sha256:76b172a539e1a44998ffd1122348d36c19fb55544a1ce49044972a94d4747ccd` | `sha256:396ae34df2beb8cdf82aeb98a42757bc4ff3d4f068acb142360b8b073cd45af8` | `813552a2e5f053cdc2df0abf6bbebc2ecfc6ae6f75245158eeb1edf70286fa26` |
| FASTT supervisor | `sha256:bff962697d6e986ba4a7c04088c0676b18690dec477d43ba3aaab0d53f08a050` | `sha256:3078c0f3ddeb92660c6fca79c6ee415e53a074b5b15666aa8ae95c3a59b1c171` | `2fa75cf2f907b55ddf55240a26bc4888c2ecbe11309aee5beec67c54e4921404` |
| Worker | `sha256:1a04a11e11827f1490b7987dfe188122e6b9f5857ec64c91eabf14a4adc8f777` | `sha256:f15fecf7b87acb0a8300e1addf443dabb90dd909aa062f388ec305a91e09c628` | `2fa75cf2f907b55ddf55240a26bc4888c2ecbe11309aee5beec67c54e4921404` |

Each image overlays only guarded text_delivery edits on its exact running predecessor. Existing pacing, contextual offers, purchase-sync, identity replay and profile work is retained. Whole-branch deployment is unsafe because production contains separate overlays.

## Operations and rollback

AWS account 323149985112, ap-southeast-2, instance i-09a9dd3bd749643a5, existing jesse-gpt profile. Manifest: /etc/twin-onlyfans/media-agency-release-compose.json. Protected host evidence: /var/lib/twin-release/paid-photo-20261030-7b6d9fb.

The first cutover stopped after worker recreation because Docker reordered HostConfig.Binds; the worker was rolled back. The retry compares sorted bind declarations plus actual source/destination/read-write mounts. Every actual environment value, command, entrypoint, resource, security option and mount remained checked. The post-rollback manifest was confirmed semantically identical before capturing its current serialization.

SSM records:
- preparation: ef1c5a2d-b29e-46a0-8e51-131b2fc764a7
- isolated PostgreSQL: 74f7452e-ac57-4e8f-b4a7-2711c8df6d33
- rolled-back attempt: d77d542d-4aee-4f14-b2f6-4761232a4ee9
- reconciliation: 668c2042-1a1f-4d06-a79f-18de82ce8be1
- successful cutover: a03f9aec-0cbe-4028-ab5f-ff5411db2faf
- final live verification: 0379f2d8-60e6-4c84-9229-ad04d825cfc2

Rollback only the affected service image key to the predecessor above after confirming it still runs this release. Retain the current environment, command, mounts and unrelated manifest changes; recreate only that service and recheck health. Do not restore an old full compose file or clear history/purchase evidence. The cutover script contains guarded failure rollback; it is not an unconditional later rollback tool.

The repository commerce/media changes reconcile the already-deployed PR #53 helper into this source tree for reproducible tests. Those files were not overwritten in the live rollout.
