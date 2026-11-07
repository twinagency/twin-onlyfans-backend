# Merge of live Moxy repair 83ead83 into engine 2688f39

Branch claude/engine-integration-20261107. Merge-base fa7f1387. Live side: 11 commits (65b048f7 .. 83ead832) from origin/codex/moxy-phrase-overlap-repair-20261107. No migration files changed on the live side.

Textual conflicts (3 files) and two clean auto-merges that needed a semantic check.

## service/onlyfans_runtime/vault_editing.py (textual)
Both sides rewrapped the same tuple of columns in the content digest; the set of keys is identical. Took the live wrapping. No behavioural difference.

## service/vault_ai_api.py (2 hunks, textual)
1. Retention of automatic visual flags when content changes (around line 2935). Engine used not vds.content_changed(item). Live compares the previously recorded _content_stamp with the current stamp and drops flags when they are known to differ, including when the current stamp is unreadable. Took live: strictly more conservative; workflow markers (_auto_folders_*) still always kept.
2. Re-check of the row under lock after the model call (around line 2992). Engine returned status stale_content; live returns status missing / content_changed with cost_millicents, and re-applies locked fields and preserve_stale_operator_descriptions. Took live: test_vault_description_coverage_pg expects content_changed and cost; the engine path that test_vault_generic_compat_pg checks (stale_content during collection) is raised earlier and is untouched. Both tests pass.

## service/vault_ai_effective.py (textual)
Both sides added _description_binding, preserve_stale_operator_descriptions and project_operator_text. Took the engine block: it is the only one that also carries description_status(), which vault_ai_api.py calls in two places (live side has no such function). Behaviour compared against live: legacy or missing stamp is never promoted to verified evidence (same); stale-stamp binding of locked descriptions (same, but engine reads effective_value); malformed _description_bindings_v1 raises in project_operator_text on the engine side whereas live silently reset it. The live and engine vault tests all pass with the engine block, so the live-visible behaviour is kept; the malformed-binding strictness is the one difference and is the safer one.

## service/onlyfans_runtime/text_delivery.py (auto-merged, edited)
Auto-merge brought in the live Moxy repairs: record_accepted may accept the receipt of a text that an operator retired as unconfirmed (status cancelled, reason operator_retired_unconfirmed_text), refuses provider-identity conflicts for it, and the dispatch-failure log line carries http_status. Engine TextAuthority one-use contract is kept: I added a guard so the retired-text acceptance never applies to an engine intent (conversation_engine.delivery.is_engine_intent); an engine attempt that is settled stays settled. Same guard added to send_recovery.snapshot, which has the same retired test.

## service/onlyfans_runtime/provider_connector.py (auto-merged)
Live change only: a non-accepted POST status is recorded on the protocol error as send_http_status for the log line. Engine did not touch that block. Kept as is.

## Files merged cleanly by git (live-only)
moxy_replacement/{conversation,parts,price_assertions,runtime,writer}.py, send_recovery.py and 7 Moxy test files.
