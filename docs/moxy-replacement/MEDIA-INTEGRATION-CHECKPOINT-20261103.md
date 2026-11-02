# Moxy media integration checkpoint

Qualified on 3 November 2026. This is an integration candidate, not an activation record.

Base: 20fc62a1ea8573bd879a2ea1c1b6a11f7d006666.
Integrated vault sync: 34471fd94f14f236b7090ef88c5befe68eabe0f5 and cdfe24e0ccf3329ed1cda5466d29ad51a1b84ab5.
Integrated description source: 5557440dae711f4256d085ec920321a496ec7d9f.
Integrated catalogue recovery: b919ca838359b3b74a16e5cd92bb308bb3c05618.

Claude currently owns the live description deployment. Do not overwrite his work or treat this snapshot as his final deployed source. Reconcile his final commit before release.

## New repair

Moxy now projects per-fan historical asset exposure from canonical outgoing Messages and VaultSend records, independent of catalogue ID. Paid attachment overlap requires an explicit resend request. Reused free previews remain allowed. Existing ownership and uncertain-payment protections remain in force. History unreadable for media blocks commercial action, while ordinary replies remain available. Snapshot and dispatch revalidation consume the same evidence.

## Tests

Final targeted combined selection: 106 passed, exit 0, pytest 323.00 seconds. Disposable PostgreSQL, synthetic models/transport. No real customer messages or paid model calls in this qualification. Earlier counts overlap and are not additive.

Included modules:
- test_moxy_exposure_audit_pg.py (18 regressions; previously 8 failed)
- test_moxy_exposure_boundaries_pg.py (12 cases covering queue races, preview reuse, explicit resend, missing media_count, malformed history)
- test_moxy_integrated_pg.py
- test_moxy_media_descriptions_pg.py
- test_vault_description_coverage_pg.py
- test_vault_sync_completion_pg.py
- test_onlyfans_offer_mapping_pg.py
- test_vault_scoped_caption_key.py

Development source and exact logs: /srv/projects/moxy-final-integration-20261103 on work-server; final.log and final.exit.json. The local Git baseline in that isolated directory is only a service snapshot. Its local ancestry must not be pushed over the published repository ancestry. This published integration preserves the actual upstream parents.

## Remaining release work

Reconcile Claude's final description source and running image. Complete and account for real vault/description coverage, actual product/price/sequence mappings and purchase links. Missing VIP mapping and conflicting current prices are not approved by this code. Run real permitted model/media and approved test-account delivery qualification before enabling customer selling. Preserve schema compatibility for every process; latest observed production schema was 0110_recent_history, not the candidate's 0111_moxy_owner.

No deployment, migration or activation is performed by this commit. No production settings or customer history are included in these source/test files.
