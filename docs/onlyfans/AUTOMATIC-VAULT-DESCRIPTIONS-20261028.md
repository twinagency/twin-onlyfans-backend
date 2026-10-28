# Automatic descriptions after account import

## Cause and behavior

`automatic_imports.ensure_started` schedules content and vault reads only.
`process_vault_jobs` saves media records and cached stills. Neither invokes the
separate `caption_vault` command. That command also loads the pilot's chatting
credential even though its captions use the house vision key. An imported,
read-only account can therefore have thousands of media records and no saved
descriptions.

The native worker now starts `VaultDescriptionWorker` alongside its read loop
when automatic imports are enabled. It discovers missing descriptions directly
from current canonical vault observations. There is no separate queue or schema
migration: missing rows survive restarts and later imported pages are picked up
on subsequent passes. Each isolated child handles at most four photos/videos.
Vision and video processing never run in the inbox thread.

The child reuses FASTT's frame collector, `_describe_one`, budget enforcement and
automatic category assignment. It obtains only the verified media session; a
chatting-model credential or outgoing activation is unnecessary. The established
legacy caption bridge stays inside the serial child, narrowed to this account,
caption purpose and the existing caption models. The parent retains its worker
role. Provider API calls are GET-only and share the existing read budget and
429 backoff. Full video preparation does not hold a provider read lease.

An account-specific PostgreSQL advisory lock prevents concurrent automatic
batches. Current tenant/binding/connection revision and canonical media evidence
are checked before collection and every metadata write. A changed human edit or
locked field fences a result that was generated from an older snapshot.

## Controls and progress

- `ONLYFANS_AUTOMATIC_IMPORTS_ENABLED=1` starts this background phase by default.
- `ONLYFANS_VAULT_DESCRIPTIONS_ENABLED=0` disables this phase independently.
- `ONLYFANS_VAULT_DESCRIPTION_EXCLUDE_ACCOUNTS` preserves explicit account holds.
- Saved `vault_ai_config_json.auto_describe_on_import=false` also holds an account
  and is rechecked before committing a running result.
- Existing descriptions and locked descriptions are skipped. Temporary failures
  retry after 30 minutes; blocked video frames use the existing seven-day cooldown.
  Model refusals require operator review. Existing cost caps remain unchanged.
- The worker emits `of-vault-description.v1` summaries with fixed states,
  described/failed counts, and a `budget_paused` state. It never logs model text,
  session captures, authenticated URLs or credentials.

Before rollout, preserve the explicit stop on Maile's completed analysis using
the exclusion or saved hold. This repair does not authorize restarting that work,
rewriting original tags, increasing caps, or changing sending controls.

## Validation and release boundary

48 isolated discovery/worker lifecycle checks and 33 existing caption-writer
checks pass locally. Coverage includes imports arriving after startup, restart
idempotency, disabled outgoing AI, tenant/connection/provenance filtering,
failure cooldowns, manual descriptions, holds after the first 32 accounts,
background lifecycle, and stale-generation refusal before reads and writes.
No real provider/model call was made during these checks.

The full PostgreSQL child/advisory-lock integration and controlled live rollout
remain to be verified. This document is not evidence of deployment or completed
live description backfill. The prior category backfill remains separate.
