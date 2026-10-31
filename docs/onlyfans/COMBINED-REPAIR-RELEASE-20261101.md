# Combined OnlyFans CRM reply-recovery release

Status: deployed to production. API, worker and supervisor run the combined image, with all 18 source hashes verified in each container, API health successful and zero container restarts at promotion. Normal `db:*` account discovery is restored.

Runtime source: `bfb676ae0afde611c8b45029523587c3ad844b9f` on `claude/of-integration-20261031`. A subsequent test-only commit, `bab87142fffe1c9a7dba1307be7dc3d19b834dec`, corrects the historical migration fixture. No runtime change is introduced by that test commit.

## Resulting behavior

- An unsent turn retains or recovers its reply schedule, including the missing-input shape that held the audited backlog.
- Unresolved attempts without frozen payloads settle absent only after the existing timeout and complete provider history proving no outbound message. Uncertain history retains the attention state; recovery does not send.
- External-message suppression records `native-no-reply-external-message`; the existing one-hour hold remains.
- Placeholder instructions are stopped at the existing send boundary and enter the existing bounded model retry path. Service-loop regressions verify successful regeneration and bounded failure.
- Identity handoff closes the spent pacing schedule so subsequent input can receive a fresh schedule.
- The operations backlog excludes blocked rows for paused, taken-over, muted or held conversations.

The branch preserves the newer deployed purchase-ledger refresh, read-fairness and model/media changes. Production advanced twice during validation. Each newer source delta was compared and retained before retrying the rollout. There is one combined release branch, with no new scheduler or send authority.

## Validation

The final image passed 123 targeted ledger/recovery/pacing checks and 776 PostgreSQL service checks. The one historical migration test originally failed on both the candidate and the deployed baseline: a shared fixture already removed profile columns, while the test left later delivery-policy columns in its reconstructed 0091 database. With the fixture corrected in a test-only overlay, the original financial-retention assertions passed. That correction is committed separately.

Earlier validation on the unchanged repair sources passed 189 focused checks, 15 model checks and 776 PostgreSQL service checks. Storage acceptance passed with the original rollback modules and the omitted concurrent-delivery table names supplied to its test inventory; strict inventory equality and behavior assertions were retained.

The first one-account trial ran for 28 minutes 44 seconds. It recorded one provider-confirmed send, zero duplicate receipts and no terminal scheduling loop. A separate production deployment restarted the API and interrupted that observation. This was not an uninterrupted 30-minute pass. The temporary account exclusion was removed and normal account discovery restored while retaining the newer deployment.

The final candidate inherits the newer purchase-ledger implementation and passed a separate five-minute confirmation (303 seconds). The confirmation recorded no new provider send, zero duplicate receipts, zero missing-input terminal schedules and no child or container restart. Its 18 changed-file hashes are verified against the candidate source manifest. Promotion is guarded against concurrent image changes. Rollback restores only this release's image/build fields and temporary scope, preserving unrelated current configuration.

## Remaining limits

One generic `send-check-failed` event occurred during the first trial. Its underlying exception was not available from the retained logs; this release does not claim that error category is fixed. Live traffic during a short trial does not prove all conversation, offer or payment paths.

No sales strategy, pricing, account activation, delivery-mode setting or external-message hold policy is changed by these repairs. Eight account policies remain in gap mode. Credential cleanup/rotation, automated backups, operational alarms, capacity testing, restore testing and the full UI/payment audit remain outstanding.

## Release artifacts

The locally tested image is `sha256:fa9de13d74c27d0eb75f6cda2dfd9248da3550e6755de0030f5e2d95997b6f0b`, tagged `twin-repair:integration-bfb676a`.

A separate source-verified recovery build is stored in the existing private ECR repository as `repair-bfb676a`, digest `sha256:042ac11468ae6cec012b466294ff128121799160ed34a95a4a78c1a7ed5a004f`. It inherits the same pinned newer production base and applies the same verified repair files. It is a separately built recovery artifact, not the exact locally observed image.

Production records: `/var/lib/twin-release/integration-bfb676a/`. The protected pre-release compose backup remains under `/etc/twin-onlyfans/`. New release logs contain no environment dumps or credential values.
