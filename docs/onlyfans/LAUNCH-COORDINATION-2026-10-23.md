# OnlyFans launch coordination, 23 October 2026

Owner of this file, shared files, schema and integration: Claude (head dev,
`codex/onlyfans-offer-import`). Updated whenever a pin, owner or status changes.

## Pinned starting commits

| repo | branch | base | pin given to other chats |
|---|---|---|---|
| jesse98/twin-onlyfans-backend | `codex/onlyfans-offer-import` | `88e79248e4b504cdaad6cb6c998fad79423e971d` | `dbd09b06dace2c24829ded6bc778d7e6d2ded4f0` |
| jesse98/twin-dashboard | `codex/onlyfans-offer-import` | `244271a36031a9eaf10b1d0ea64f41d97babd80a` | `927b2d75105f6180c37f2759ed119f89521b979d` |

Verified 23 Oct: all four backend chat branches below are based exactly on
`dbd09b0`. The head-dev branch has moved on (50% checkpoint at backend
`eb242e7`, dashboard `5309b49`); the newer work only adds files the chats do
not own, so their branches merge without conflicts on owned paths.

## Branches and ownership

| chat | branch | head seen | owns (exclusive) |
|---|---|---|---|
| 1 Purchase sync / ownership | `codex/onlyfans-ledger-recovery-20261023` | `4b4a7f3` | `service/onlyfans_runtime/ledger.py`, `service/onlyfans_runtime/launch_ledger/`, `service/tests/test_onlyfans_launch_ledger*.py`, `docs/onlyfans/LEDGER-RECOVERY-2026-10-23.md` |
| 2 History recovery / fan memory | `codex/onlyfans-history-memory-20261023` | `028acda` | `service/onlyfans_runtime/launch_history_memory/`, `service/tests/test_onlyfans_launch_history_memory*.py` |
| 3 Personality / AI configuration | `codex/onlyfans-personality-20261023` | `5319fc5` | `service/onlyfans_runtime/launch_personality/`, `service/tests/test_onlyfans_launch_personality*.py` |
| 4 Proactive selling / follow-ups | `codex/onlyfans-proactive-selling-20261023` | `5f292e2` | `service/onlyfans_runtime/launch_engagement/`, `service/tests/test_onlyfans_launch_engagement*.py` |
| 5 Launch dashboard | `codex/onlyfans-launch-dashboard` (dashboard repo, separate Claude session) | from `927b2d7` | `components/onlyfans-launch/`, `lib/onlyfans/launch/`, `app/api/onlyfans-launch/`, `scripts/check-onlyfans-launch*` |
| Head dev (Claude) | `codex/onlyfans-offer-import` (both repos) | backend `eb242e7`, dashboard `5309b49` | importer, fixed price, content sync; shared worker/store/connector/planning; `service/db/*` models, migrations, `schema.py`; `onlyfans_app.py` mounting; `agency_scope.py`; dependency files; CI; dashboard navigation, layouts, `NativeWorkspace.tsx`, shared `lib/onlyfans/*` contracts, `package.json`, `ci.yml` |

Overlap check (23 Oct, by diff against `dbd09b0`): no chat edits a head-dev
file. Only Chat 1 edits an existing shared file, `ledger.py`, which it owns
while working. No handover needed yet. Any chat needing a shared-file change
sends the exact change; the head dev applies it.

## Schema

- Native head on the head-dev branch: `0089_offer_import` (unpublished;
  import evidence, `catalog_items.pricing_mode`, content-sync tables).
- Chats 2, 3 and 4 deliver table contracts in their own folders
  (`launch_history_memory/models.py` + `schema_contract.py`,
  `launch_personality/models.py` + `schema_contract.py`,
  `launch_engagement/models.py` + `schema_contract.sql`). They do not register
  models or add migrations themselves.
- Integration plan: one new linear revision `0090_launch` (parent
  `0089_offer_import`) with all chat tables, registered in `schema.py`
  (rank 90, append-only/frozen guards checked at startup). No competing heads.
- Chat 1 currently adds no tables. Its upgrade test helper references
  `ck_catalog_items_pricing_mode` (an 0089 object) and must follow the head
  when 0090 lands.

Proposed tables to fold into 0090:
- Chat 2: `of_history_memory_state`, `of_history_memory_receipt`,
  `of_history_memory_evidence`, `of_history_memory_correction`,
  `of_history_recent_bootstrap`, `of_launch_history_scan`, `of_launch_history_cursor`.
- Chat 3: `of_personality_head`, `of_personality_revision`.
- Chat 4: `of_engagement_settings`, `of_engagement_settings_change`, `of_engagement_action`.

## API contracts

| contract | prefix | header | owner | status |
|---|---|---|---|---|
| of-offer-import.v1 | `/onlyfans/offer-import/v1/accounts/{id}` | `X-OnlyFans-Offer-Import-Version` | head dev | built, tested |
| of-content.v2 | `/onlyfans/content/v2/...` | content v2 header | head dev | built, tested |
| of-content-sync.v1 | `/onlyfans/content-sync/v1/accounts/{id}` | `X-OnlyFans-Content-Sync-Version` | head dev | built, tested; see `CONTENT-SYNC.md` |
| ledger recovery | see `docs/onlyfans/LEDGER-RECOVERY-2026-10-23.md` | | Chat 1 | delivered on branch, not integrated |
| history/memory | `launch_history_memory/` | | Chat 2 | delivered on branch, not integrated |
| personality | `launch_personality/api.py`, `dashboard-contract.ts` | | Chat 3 | delivered on branch, not integrated |
| engagement | `launch_engagement/INTEGRATION.md`, `contracts.py` | | Chat 4 | delivered on branch, not integrated |

## Live read diagnostics (stored creator session, read-only, redacted)

- 23 Oct: connection verified (revision 2); prod schema 0088; 0 catalogue
  items; 1,064 vault items; ~351k priced outgoing messages.
- Ledger offset 40: 14 valid rows + 6 rows outside the requested 90-day
  window; `parse_page` refuses the whole page, scan held. Response has
  `marker`/`nextMarker`. 52 applied ownership rows, 0 messages marked paid.
  Sent to Jesse for Chat 1.
- Paid messages carry `previews` and `lockedText`. Paid-post read contract
  not yet verified (first request non-200).
- History recovery status: not yet read (next authorized window).

## Integration status

| part | code complete | integrated | deployed | live verified |
|---|---|---|---|---|
| Offer import + fixed price | yes (offline) | yes | no | read-only shapes only |
| Content sync (15) | yes (offline); upload transport blocked (no unpublished path) | yes | no | no |
| Chat 1 ledger | delivered | no | no | no |
| Chat 2 history/memory | delivered | no | no | no |
| Chat 3 personality | delivered | no | no | no |
| Chat 4 engagement | delivered | no | no | no |
| Chat 5 launch dashboard | in progress (separate session) | no | no | no |
