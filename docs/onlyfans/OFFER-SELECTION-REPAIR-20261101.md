# Offer selection repair, 1 November 2026

Status: tested locally and prepared for deployment. Not deployed; no live provider send was performed.

Base: cca077057fb01185fab25bc5755cebb514976aef, the combined repair branch documenting runtime bfb676a. Recheck the actual live revision before release and preserve any newer changes.

## Behavior

A broad request such as "show me something tonight, what do u have for me" excludes previous paid-card media before searching, instead of treating any overlap as a repeated-item request. Explicit same-item intent is read in the existing contract call; only a matched, recorded card can produce item_already_pending. Unidentified references do not create a different sale.

Both contextual and curated selectors exclude prior media, including contextual origins whose vault_sends projection is missing. The final media-identity guard remains authoritative. Relevant different content remains eligible after a confirmed unpaid contextual card. Reserved/unknown delivery without a matching positive receipt still holds the fan. No old offer is falsely marked purchased or cancelled.

The database unique index now covers reserved/unknown deliveries rather than all sent cards. Sent cards and their media remain available to duplicate checks. The follow-up chasing suppression remains in place.

## Validation

- 111 focused pytest tests passed (selection, contextual authority, evidence).
- Both new incident regressions fail against the unpatched selection and authority code.
- Five real FASTT engine scenarios passed using synthetic model/provider responses: off, shadow, live, fixed-price live, and the exact broad request with a prior unpaid file. Each live scenario sends exactly one fresh paid card; off/shadow send none.
- Real PostgreSQL index migration test passed: multiple sent cards retained; concurrent unresolved delivery rejected; unsafe downgrade refused.
- Full frozen-baseline PostgreSQL bootstrap and schema check passed at 0104_contextual_item_scope.
- Python compilation passed.
- Live account settings, server logs, deployed image and actual OnlyFans delivery are not verified by these local tests.

## Release

Build from this branch after reconciling the actual live source. Drain active delivery; coordinate API, worker and supervisor because schema version checks are strict. Use the normal db.schema upgrade procedure from 0103 to 0104, then deploy all roles from the same image. Preserve current account scope and configuration. Verify startup health, schema, no unknown attempts, and one approved inbound test.

Do not simply redeploy an old 0103 image after migration. The downgrade refuses if multiple sent/unresolved rows exist for one fan; it never deletes or falsifies delivery records. Prefer a forward repair if rollback would lose the new index semantics.

Available AWS CLI returned NoCredentials, so production migration and deployment could not be executed from this session.
