# OnlyFans priority work: audience validation foundation

## Scope and result

This is the first contained implementation slice of Jesse's 26 October priority plan.
It hardens the inherited Smart Lists boundary before that code is exposed in the current OnlyFans dashboard or campaign builder.

Corrupt saved definitions now stop with HTTP 409. Unsupported definitions and invalid numeric thresholds stop with HTTP 422 on write/direct resolution. The resolver validates the complete definition before evaluating fans. Returning an empty membership set is deliberately avoided for invalid saved rules because an invalid list might be an exclusion.

Valid legacy blank definitions retain their intentional everyone semantics. Existing valid numeric/string/tag predicates, match-all/match-any logic, supported window clamping and successful response shapes are retained.

## Checked baseline

- Parent source: backend 14998d9eb772043b8a676f1058c8756a3d38517f, PR #29.
- Backend default branch remains older than the integrated production release; it is not a safe release base.
- Current Smart Lists source hash in the API: d58357b45da30e10aefb989753f0e734a9588e339fee1b52c9523d39fd78b196. This matches the file inspected for this patch.
- Current source callers checked: server.py router, saved-list preview/resolve/serialization, and automations/auto_follow.py.
- Current app entry point is service.onlyfans_app:app. It does not mount the inherited Smart Lists router. This patch alone does not expose audiences to the current dashboard.
- Current campaign path still accepts explicit/all_known audiences and has a 1,000-recipient cap.
- No new provider handler, worker, schedule, model call or prompt instruction is introduced.

## Live read-only checks on 26 October 2026 AEST

SSM inventory 259a5697-db32-4e77-a132-79bcb4c64dec and source inspection 762b91c4-39fb-4e52-91ed-e6a8af58e04f confirmed separate API, worker and FASTT supervisor images:
- API: ce1ba4221a97b5a7faa4e438050aeffbae6086aa984d69877e8a520d27a1bf09
- Worker: 8d50e4fa3c0c0b3942770e61bcea744fb3cb450501e011744275ab9ecc703ca8
- FASTT: e721d649e631f52a8ea6b642c001f8a2584b8e687baa6c3bd4b91db02867a714

The initial hash command used the wrong working directory and failed. It was corrected to /app/service; the subsequent command returned the requested hashes. Its final optional file glob exited 2 because no vault-sync test file existed in that API image. This is not a passing live test-suite result.

Read-only schema check 1b0e82fb-678d-46a7-9c2a-dd1df8759494 passed:
OnlyFans schema check: ready (0092_ledger_history).

The worker's vault_management.py and provider_connector.py fingerprints matched the inspected vault-resume PR #30 checkout. The API has different versions of those files. Do not deploy an older monolithic image over these integrated releases.

Read-only saved-list compatibility audit f148c08f-5909-4c9a-bf66-c58c41b9a6ac:
- saved_list_total: 0
- invalid_under_new_validator: 0
- valid_blank_everyone: 0

The audit used a transaction explicitly set READ ONLY and reported counts only. It did not modify records or send messages. There are no existing saved lists in this database requiring repair. Compatibility for imported/legacy definitions is covered synthetically, not established from existing production records.

## Pending changes and interactions

Open PRs refreshed before editing:
- #30 vault-resume, #29 chat media, #28 full ledger history.
- #24 bounded concurrency/private lanes.
- #26 long vault sync and #22 large folder lists.
- Dashboard #295 Moxy permissions, #294 agency saves, #293 browser sign-in, #289 media, #288 takeover, #273 delivery intervals.

The scale and ledger-history branches both have Alembic down_revision 0091_launch_controls, with different 0092 heads. Production is on 0092_ledger_history. Future schema changes must start from the reconciled deployed chain; the scale migration must not be blindly applied as an unrelated head.

The existing auto_follow caller imports _rules_of and resolve. Invalid definitions now raise rather than produce everyone. Its existing per-source exception handling remains responsible for whether independently configured pools continue. No new mass-message or campaign integration is enabled.

## Prompt and displayed configuration review

Inspected ai_chatter._runtime_prompt_context / _build_messages, NativeEngine.load_config, TwinPromptContext and the dashboard ConfigurationEditor/clone prompt-preview distinction during the plan and baseline audit.
Recent relevant history includes 0989063 (inflight fan messages), 874b3c9 (post-buy/discount fixes), 0225b50 (discount and post-buy switches) and 032156d (resolver/vision seam).

The patch does not modify any of those components or their configuration. Raw source fingerprints of the running FASTT files differ from the isolated parent checkout, so this document does not certify that the whole runtime matches that checkout. The audience-validation file was checked directly. No deployment of this older checkout as a complete release is appropriate.

## Verification and limits

Run from service with an isolated synthetic database:
TWIN_FASTT_CONFIG_ISOLATED=1
DATABASE_URL=sqlite+aiosqlite:///:memory:
ONLYFANS_PROCESS_ROLE unset

python -m pytest tests/test_smart_lists_safety.py -q
Result: 34 passed.

Cases cover:
- Corrupt JSON, null/scalar/array definitions and unsupported fields/versions.
- Invalid match/rules/operator shapes and nonfinite/boolean numeric thresholds.
- Invalid saved rules block the resolve route before the fan loader.
- Direct resolution refuses invalid rules before iterating the fan collection.
- Valid legacy blank, numeric, recent-spend, status, tags and any-match definitions.

git diff --check passed.

This patch changes no database structures, financial interpretation, purchase-history coverage, response pagination or approved bot behaviour. No data migration/backfill is required. Full PostgreSQL feature/concurrency tests belong to the later data/campaign stages; they are not claimed here.

Rollback is a source revert of this patch with no database downgrade. A reverse-apply check is recorded with the commit verification. Reverting also restores the old unsafe fallback, so keep the current dashboard audience integration disabled until the guard or an equivalent fix is present.

## Remaining work

The four requested features are not completed by this patch.
Next work must establish coverage-aware fan/content facts on the actual integrated release, then provide scoped Smart Lists APIs/UI and campaign audiences. Missing transaction coverage still normalizes to zero in the legacy loader; that limitation is intentionally not reinterpreted without the shared facts contract and its UI compatibility work.

Scripts still require real OnlyFans inbox integration and durable receipt-linked progress. Campaign priority/scale and sequences still require common sender arbitration. Vault performance still requires canonical purchase attribution with bundles counted once.

