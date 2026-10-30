# Bulk AI controls and creator discounts, 30 October 2026

Backend source cb937394e0c9bd914b499080d5913b9b0342c5f3, PR65. Dashboard source 2f98dd5a1f629db17e14bdf03cd960382d9cf432, PR340.

The existing-chat enable button previously performed full per-chat reads and individual writes, stopped at the first refusal, and skipped paused conversations with prior control revisions. The new helper reports discovery and batch progress, uses bounded batches of 50, supports enable and disable, and records explicit skipped reasons. It freezes the discovered list before changes. New fans remain default-off. A lost response stops further batches and reports uncertainty.

The API uses existing canonical Store.control transactions for state, runtime stop, work invalidation and audit. Every fan is revision checked. Bulk actions preserve takeovers, no-contact, individual review, muted-fan, unresolved-delivery and stronger runtime restrictions. Management and creator scope are checked. No new sender or migration is introduced.

## Completed production changes

All five creators now have the existing fastt_discount_resend switch enabled. Katie revision4→5 and Alva revision3→4 changed; Nicky revision7, Maile revision11 and Naomi revision11 were already enabled. The canonical configuration authority checked revision/fingerprint, wrote an audit, and verified that only this switch changed. Existing discount pricing/qualification, account pause, delivery policy and budgets remain unchanged. Nicky and Katie remain paused. SSM 37757231-0f77-478f-bfcc-07346378a142 completed successfully.

Full dashboard TypeScript checking also passed against the exact selected source on the connected workstation.

API deployed as sha256:87b580750f3bc20a2ba3305c021357f9e6f957127ac79c6a6b9b215dc656547f, derived only from the previous live API sha256:396ae34df2beb8cdf82aeb98a42757bc4ff3d4f068acb142360b8b073cd45af8. Guarded contextual source changes preserve all other overlays. Worker and FASTT supervisor were not redeployed.

Preparation/test SSM: 375cdbdd-9c07-4784-9489-1daf5a801546. Cutover SSM: 4a53c05a-d9df-4b48-8a89-6fafa767821b. Both succeeded. Health and authenticated readiness returned200; the authenticated new endpoint rejected an empty batch before writes. Every runtime environment value, command, resource, mount and security setting was compared. The prior paid-photo source remained byte-identical.

## Verification

29 PostgreSQL tests passed in 86.54 seconds against the exact candidate source in a compatible test dependency image. Tests used a disposable PostgreSQL16 instance on an internal Docker network, synthetic data/credentials, resource bounds, and no mounted production data. Both were removed after tests. Coverage includes 50-fan pause/resume at nonzero revisions, exact historical retry, runtime stop projection, race refusal followed by success, protected fan reasons, management and creator scope, strict HTTP validation, and the existing control/runtime suites.

The 100 canonical pause/resume controls completed in 1.15 seconds on the isolated server.

The full test:onlyfans-ui suite passed on the connected workstation, including the new bulk suite and existing chat media, AI visibility, permissions, inbox refresh, pagination, session and campaign tests.

The frontend synthetic suite passed locally, covering 50/50/5 batching, inverse action, progress, nonzero revisions, skip continuation, malformed/lost acknowledgements, cancellation, pagination/account isolation and credentialed proxy behavior.

No test messages, paid media or real fan control changes were sent. Actual user-triggered account-wide completion is not claimed. Dashboard release status will be appended after verification.

## Recovery

Protected host evidence: /var/lib/twin-release/bulk-ai-20261030. Full runtime/container snapshots are retained only there and are not included in this repository. An API predecessor image archive is saved there for rollback. To reverse, verify no concurrent image or manifest changes, restore only the API image/command in /etc/twin-onlyfans/media-agency-release-compose.json, and recreate only api with --no-deps. Do not deploy this backend branch wholesale; production contains separate retained overlays.
