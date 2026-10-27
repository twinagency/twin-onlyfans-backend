# Launch integration, 23 October 2026

Branch `codex/onlyfans-launch-integration` = head-dev `codex/onlyfans-offer-import` (b588b97) + the four chat branches.
Nothing here enables sending, activation or provider polling by default.

## Schema
One linear revision `0090_launch` (parent `0089_offer_import`): `db/migrations/versions/20261023_0200_launch.py` runs
`db/snapshots/0090_launch.sql`. This original combined revision has rank 90;
the final additive head is rank 91 below. `OFFER_IMPORT` keeps the 0089 name. Original tables (all `onlyfans_since=0090_launch`):
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

## Final integration additions

The final integration extends the existing linear schema with `0091_launch_controls`,
after `0089_offer_import` and `0090_launch`. It retains prior evidence and adds durable
launch commands, the concurrent immutable ledger-retry audit, exact-evidence ledger resolutions, source occurrence/progress,
account-wide provider-read budgets and actual worker observations. There is no
automatic downgrade or implicit account activation.

Configuration-purpose history-memory and engagement routes now provide stable scoped
fan/action pages. The dashboard uses the existing configuration authority for those
routes and the document-level personality workflow. The connection authority supplies
non-secret credential readiness for the exact saved model. Budget and reasoning edits
use the existing revision/fingerprint/audit configuration writer.

Read-purpose `/onlyfans/api/v1/accounts/{id}/readiness` and the canonical flat
`/ledger` expose stored status; `/ledger/evidence` adds paginated investigation
evidence. Control-purpose `/onlyfans/control/v1/accounts/{id}/control`, `/ledger/retry`
and `/ledger/resolve` record durable commands. Retry compares the current connection
revision and complete scan fingerprint and uses one retry authority. It acknowledges scheduling, and
resolution requires existing provider transaction/message evidence. Neither grants
arbitrary ownership. Account resume revalidates prerequisites and leaves old work blocked.

Live inbound replies, conversation controls, verified purchases/reversals, connection
changes and catalogue mutations invalidate engagement within their existing transactions.
Final dispatch still revalidates, and uncertain sends remain uncertain.

Provider-read leases/backoff cover recent/discovery/history, ledger, inventory refresh,
post scans and vault sync, with current-session and lease fencing at result commits.
Carrier uploads require the explicit account allowlist plus current binding, asset/lease,
role and a fresh private dashboard permission decision at each external boundary.
Absent authority holds the operation. No new carrier account is authorized by this code.

Paid-post historical continuation stays partial until a bounded read-only provider
contract validation records current-binding evidence. Historical memory remains
conservative English pattern extraction and lexical, source-linked retrieval.

Passing targeted tests is implementation evidence only. The final execution record
tracks the paired candidate, complete acceptance, deployed digests and real-account
verification separately; no deployment or activation follows merely from these changes.
