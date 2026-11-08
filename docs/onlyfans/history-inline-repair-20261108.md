# OnlyFans history inline fragment repair, 8 November 2026

Legacy history messages can contain ordinary text and a balanced inline link without a paragraph wrapper. The history parser refused that representation, leaving the history page at its checkpoint.

The repair supplies a paragraph container and uses the existing bounded history parser. Unknown tags, malformed nesting and nested links remain rejected. The exact outgoing message and receipt parser is unchanged.

## Verification

- Four representative valid fragments failed before the repair.
- 160 focused tests passed after the repair, covering history, protocol and provider adapters.
- Separate candidate images based on the actual running worker and importer each passed 17 parser assertions with networking disabled.
- An isolated live history read using the repaired parsing behavior returned nine messages.
- The production worker and history importer were replaced with those verified candidate images.
- After cutover, both containers were running with zero restarts and the exact tested parser hashes.
- Runtime environments, commands, entrypoints, mounts, networks and restart policies were preserved. The API container and its startup time were preserved.
- No manual customer message or paid model test was sent.

## Source and operations

Source repair commit: `238fdf73a55bb9e75c72e1602df323a68b63a1d4`.

The live images derive from the existing production images. This does not deploy Plan C or replace the rest of either service with a different branch.

Production release record: `/var/lib/twin-release/history-inline-20261108/release.json`.

The release directory contains the tested parser copies, build and test records, the scoped Compose override and `rollback.sh`. Rollback restores the preceding images with the preserved runtime settings; it does not restore an old database.

The existing importer's runtime value for `ONLYFANS_FAN_CURRENT_OVERRIDE` was preserved explicitly because it differed from the current Compose file.

## Remaining issue

Amelia has a separate held event with reason `moxy_generation_reconciliation_required`. This parser change does not resolve that generation or clear the hold. Other causes of history failures, including rate limits and unavailable threads, require their own evidence.
