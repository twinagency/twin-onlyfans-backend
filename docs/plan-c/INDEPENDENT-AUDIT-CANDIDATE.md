# Independent audit candidate — NOT RELEASE READY

This isolated branch starts at cdae299 and preserves the previous builder's nine-file dirty sales-journey patch and its tests. The original builder and dashboard checkouts were not edited. V1's source snapshot stays fixed; this candidate is a separate V2 update.

## Narrow repairs
- Give the writer the standing approved one-photo grant on an otherwise eligible open-sales turn, rather than requiring a magic purchase sentence. Missing commercial evidence does not stop ordinary conversation. Pause, ownership, frozen terms and delivery checks remain authoritative.
- Recognize an explicit "Actually, I want to buy the photo" change of mind within the existing narrow current-request detector; hypothetical/quoted/referenced requests do not reopen selling.
- Recover only proven-unsent autonomous text drafts after listed transient invalidations. Cancel queued stale work through existing reservation accounting, regenerate from current context and retain stop/takeover/unknown barriers.
- Expire model leases through the existing ledger. Pre-wire reservations can recover; calling/unknown outcomes retain uncertainty and budget liability.
- Avoid a stale parallel worker changing request revision after the previous generation was already retired.
- Share the identity-question detector between journey and rendered writer classification. Incidental agency/chatter mentions no longer force identity wording. Direct identity questions still require the approved truthful line.
- Narrow reproduced incidental-email and unrelated-expense signals. Preserve direct contact requests, explicit refusal, affordability and sales opt-out. Finite language tests are not a semantic completeness guarantee.

## Known failing journey — do not hide it
`test_independent_audit_pg.py::test_queued_offer_correction_can_finish_without_reusing_sent_authority` demonstrates: an offer is queued, memory changes, the old intent is correctly cancelled, but its consumed grant prevents the same request finishing. The request remains open/review_offer_authorization and no paid card is sent.

The existing scope explicitly forbids automatic renewal of consumed or expired grants. This audit does not reset used_intent_id, remove uniqueness, extend expired coverage or reuse an uncertain send key. A repair needs an explicit, audited replacement-authorization design for a proven-never-dispatched cancelled card. It must retain old grant/intent linkage; recheck current permission, scope, exact terms, media, ownership, coverage, stops and takeover; serialize concurrent replacements; and refuse replacement after any wire uncertainty. Until implemented and tested, autonomous sale recovery FAILS.

## Additional unresolved gates
Unsupported/malformed signed events can create a permanent safety hold; an audited evidence-based release workflow is missing. Unknown model calls have no qualified operator resolution workflow. Outside-scope digest capacity/retention remains bounded at 10,000 with unqualified overflow handling. Administrator offboarding, actual writer/provider isolation, real bindings/model identity and quality, restore-tested backup and compatible rollback remain unqualified. Do not bypass any protection to make a test green.

No production deployment, provider/model paid calls, customer messages, purchases or authorization changes were made by this audit. Broad and browser test results belong to the external evidence directory and must be matched to the final image/commit. This document alone is not a test result.
