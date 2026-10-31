# Requested discounts, 31 October 2026

A single explicit request now qualifies for a discount without the former 90-minute age gate or a second fan message. The first reduction defaults to 15%, with a hard 20% ceiling and existing price floors. Silence does not authorize a reduction. Declines, distress, paid/owned media, human takeover, payment holds and uncertain card removal continue to block delivery. One item cannot be repeatedly discounted. A request targets the latest open card rather than all earlier unpaid cards.

Production image: `twin-requested-discounts:20261031-9c026-v2`, SHA256 `ace28565f1d9412fa3503bdbd72219c863d4591d5677203fa34a1fe60536ca92`. It is derived from the live `twin-reliability-supervisor:20261031-offer-capacity-v6` image, preserving that concurrent deployment. Only the supervisor was redeployed. API and worker configuration were preserved.

Validation: five policy tests and 277 engine integration checks passed in network-isolated candidate containers. The loaded production hashes of all six changed modules match the tested image; the service is running with zero restarts and no fatal/traceback entries in the checked startup logs. The $6.70 synthetic card became $5.70 on one request; the 20% ceiling is $5.36. This is not a claim that the historic live test card has already been replaced.

Reproducible patch: `python deploy/apply_requested_discounts.py <service-source-root>`. It edits named functions with compatibility assertions rather than copying an old full source tree. `deploy/update_requested_discount_tests.py` updates the existing integration fixture's old expectations before testing a live snapshot. Source on this branch is patched from its own repository base, while the deployed image was patched from the current live image.

Server release record and rollback override: `/opt/twin-onlyfans/requested-discounts-20261031-9c026-v2/`. `rollback-command.json` contains the exact command for the previous image. Preserve the current image/configuration lease before a later deployment or rollback. The placeholder-text reply defect is separate from this pricing change.
