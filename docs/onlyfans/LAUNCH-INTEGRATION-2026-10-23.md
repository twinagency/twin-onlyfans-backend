# Launch integration, 23 October 2026

Branch `codex/onlyfans-launch-integration` = head-dev `codex/onlyfans-offer-import` (b588b97) + the four chat branches.
Nothing here enables sending, activation or provider polling by default.

## Schema
One linear revision `0090_launch` (parent `0089_offer_import`): `db/migrations/versions/20261023_0200_launch.py` runs
`db/snapshots/0090_launch.sql`. HEAD rank 90; `OFFER_IMPORT` keeps the 0089 name. Tables (all `onlyfans_since=0090_launch`):
history/memory (`of_history_memory_{state,receipt,evidence,correction}`, `of_history_recent_bootstrap`, `of_launch_history_{scan,cursor}`),
personality (`of_personality_{head,revision}`), engagement (`of_engagement_{settings,settings_change,action}`).
`of_text_intent.origin` also permits `engagement` (same pattern as 0080 campaigns). The chat packages now define their tables on the shared
`Base` (`db/onlyfans_launch_models.py` only registers them); constraint names follow the metadata naming convention.
`check_schema` verifies four append-only triggers and a hashed-body check of `of_engagement_action_guard`.

## Worker flags (all default off)
`ONLYFANS_HISTORY_RECOVERY_ENABLED` (recent cutover + archive recovery), `ONLYFANS_HISTORY_MEMORY_ENABLED` (offline memory
processing), `ONLYFANS_ENGAGEMENT_ENABLED` (schedule + queue; also needs saved per-account engagement settings, which default disabled).
Reads of memory in prompts/snapshots (`enrich_snapshot`) are always on but add prompt text only when older excerpts or operator
overrides exist.

## API (configuration purpose, existing token/header/agency rules)
Personality (mounted from the package), engagement (`/engagement`), history memory (`/history-memory`) under
`/onlyfans/configuration/v1/accounts/{id}/...` in `onlyfans_commands/launch_api.py`.

## Not applied
See the integration report; ledger dashboard read/retry, event-fed engagement cancel and the personality credential reader remain open.
