#!/usr/bin/env bash
# One capped runner at a time; isolated PostgreSQL network only.
set -euo pipefail
project=/srv/projects/twin-conversation-engine-20261107
image=${ENGINE_TEST_IMAGE:-twin-engine-offline-main:20261107-r3}
pg=twin-engine-lab-pg-capped-20261107
[[ "$(hostname)" == ubuntu-s-4vcpu-8gb-syd1 ]]
[[ -d "$project/backend/.git" && -d "$project/dashboard/.git" ]]
[[ "$(docker inspect "$pg" --format '{{.HostConfig.NetworkMode}}')" == none ]]
[[ "$(docker inspect "$pg" --format '{{.State.Running}}')" == true ]]
common=(--rm --pull never --cpus 0.75 --pids-limit 96 --cap-drop ALL --security-opt no-new-privileges --read-only --user 1000:1000 -e HOME=/tmp)
docker run "${common[@]}" --network none --memory 768m --tmpfs /tmp:rw,nosuid,size=96m \
  -e PYTHONPATH=/opt/test-packages:/app/service:/app/service/tests -e PYTHONDONTWRITEBYTECODE=1 -e PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  -v "$project/test-packages:/opt/test-packages:ro" -w /app --entrypoint /usr/local/bin/python "$image" \
  -m pytest -q --disable-warnings --maxfail=1 -p no:cacheprovider service/tests/conversation_engine service/tests/test_moxy_description_freshness.py service/tests/test_vault_recent_window.py \
  > "$project/evidence/continuation-unit-regressions.log" 2>&1

docker run "${common[@]}" --network "container:$pg" --memory 1024m --tmpfs /tmp:rw,nosuid,size=128m \
  -e PYTHONPATH=/opt/test-packages:/app/service:/app/service/tests -e PYTHONDONTWRITEBYTECODE=1 -e PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  -e ONLYFANS_TEST_PG_EMPTY_TEMPLATE=1 -e ONLYFANS_TEST_PG_URL=postgresql://twin_lab@127.0.0.1:5432/postgres \
  -e ENGINE_JOURNEY_ARTIFACT=/evidence/synthetic-journey-qualified.json -e ENGINE_SEQUENCE_ARTIFACT=/evidence/sequence-projection-qualified.json -e ENGINE_OFFER_ARTIFACT=/evidence/synthetic-offer-qualified.json \
  -v "$project/test-packages:/opt/test-packages:ro" -v "$project/evidence:/evidence" -v "$project/backend/compat:/app/compat:ro" -w /app --entrypoint /usr/local/bin/python "$image" \
  -m pytest -q --disable-warnings --maxfail=1 -p no:cacheprovider \
  service/tests/test_engine_catalogue_pg.py service/tests/test_engine_controls_pg.py service/tests/test_engine_independence_pg.py \
  service/tests/test_engine_memory_pg.py service/tests/test_engine_offer_pg.py service/tests/test_engine_operator_pg.py \
  service/tests/test_engine_sequence_pg.py service/tests/test_engine_simulated_journey_pg.py service/tests/test_fans_local_http_pg.py \
  service/tests/test_vault_generic_compat_pg.py \
  service/tests/test_onlyfans_foundation_pg.py::test_bootstrap_and_refusal_are_explicit \
  service/tests/test_onlyfans_foundation_pg.py::test_populated_baseline_upgrade_preserves_evidence \
  service/tests/test_onlyfans_foundation_pg.py::test_control_invalidates_claim_and_retains_unknown \
  service/tests/test_onlyfans_controls_pg.py::test_controls_replay_is_historical_and_no_contact_cannot_be_reversed \
  service/tests/test_onlyfans_controls_pg.py::test_takeover_waits_for_inflight_proof_and_completes_once \
  service/tests/test_onlyfans_controls_pg.py::test_later_no_contact_and_retained_unknown_evidence_prevent_takeover_completion \
  service/tests/test_onlyfans_controls_pg.py::test_two_process_controls_serialize_and_replay_without_duplicate_evidence \
  service/tests/test_vault_recent_window_pg.py::test_populated_additive_migration_preserves_existing_rows \
  service/tests/test_vault_recent_window_pg.py::test_approved_older_product_outside_window_keeps_existing_sale_path \
  service/tests/test_moxy_media_descriptions_pg.py::test_operator_correction_beats_model_description_in_prompt \
  service/tests/test_onlyfans_vault_editing_pg.py::test_description_review_binds_current_content_and_rejects_stale_view \
  service/tests/test_vault_description_coverage_pg.py::test_forced_model_refresh_cannot_renew_a_stale_legacy_operator_correction \
  service/tests/test_vault_description_coverage_pg.py::test_automatic_visual_flags_are_not_carried_to_changed_content \
  service/tests/test_onlyfans_text_delivery_pg.py::test_manual_success_is_scoped_idempotent_and_projects_only_provider_proof \
  service/tests/test_onlyfans_text_delivery_pg.py::test_automated_receipt_records_ai_origin \
  service/tests/test_onlyfans_text_delivery_pg.py::test_ambiguous_wire_holds_budget_and_never_automatically_resends \
  service/tests/test_onlyfans_text_delivery_pg.py::test_expired_dispatch_becomes_unknown_and_late_matching_proof_settles \
  service/tests/test_onlyfans_text_delivery_pg.py::test_projection_crash_is_repaired_without_a_second_wire_call \
  service/tests/test_onlyfans_text_delivery_pg.py::test_two_processes_dispatch_one_intent_and_reserve_one_unit \
  > "$project/evidence/continuation-postgres-regressions.log" 2>&1

docker run "${common[@]}" --network none --memory 1200m --tmpfs /tmp:rw,nosuid,size=128m -e NODE_OPTIONS=--max-old-space-size=900 \
  -e ENGINE_JOURNEY_ARTIFACT=/evidence/synthetic-journey-qualified.json -e ENGINE_OFFER_ARTIFACT=/evidence/synthetic-offer-qualified.json \
  -v "$project/dashboard:/workspace:ro" -v "$project/evidence:/evidence:ro" -v /usr/local/bin/node:/usr/local/bin/node:ro -w /workspace --entrypoint /bin/sh "$image" -c '
    set -eu
    node --import tsx --test tests/contracts/facade.test.mjs tests/contracts/engine-status.test.mjs tests/contracts/engine-operator.test.ts
    node --import tsx tests/contracts/full-journey-render.tsx
    node --import tsx tests/contracts/offer-journey-render.tsx
    node --import tsx scripts/check-onlyfans-read.ts
    node --import tsx scripts/check-onlyfans-ui.tsx
    node --import tsx scripts/check-chat-history-window.ts
    node --import tsx scripts/check-live-telegram.ts
  ' > "$project/evidence/continuation-dashboard-regressions.log" 2>&1

docker run "${common[@]}" --network none --memory 1536m --memory-swap 1536m --tmpfs /tmp:rw,nosuid,size=256m -e NODE_OPTIONS=--max-old-space-size=1200 \
  -e ENGINE_BROWSER_OUTPUT=/evidence/browser-engine-qualified -e ENGINE_BROWSER_ARTIFACTS=/evidence/browser-engine-results-qualified \
  -e ENGINE_JOURNEY_ARTIFACT=/evidence/synthetic-journey-qualified.json -e ENGINE_SEQUENCE_ARTIFACT=/evidence/sequence-projection-qualified.json \
  -e ONLYFANS_UI_OUTPUT=/evidence/browser-preview-qualified -e ONLYFANS_UI_ARTIFACT_DIR=/evidence/browser-results-qualified \
  -e PREVIEW_CHROME=/browser/chromium_headless_shell-1228/chrome-headless-shell-linux64/chrome-headless-shell \
  -e LD_LIBRARY_PATH=/browser-libs/usr/lib/x86_64-linux-gnu:/browser-libs/lib/x86_64-linux-gnu \
  -v "$project/dashboard:/workspace:ro" -v "$project/evidence:/evidence" -v /home/worker/.cache/ms-playwright:/browser:ro \
  -v /srv/projects/moxy-browser-libs/extract:/browser-libs:ro -v /usr/local/bin/node:/usr/local/bin/node:ro -w /workspace --entrypoint /bin/sh "$image" -c '
    set -eu
    node tests/browser/build-engine-journey.mjs
    node tests/browser/check-engine-journey.mjs
    node preview/onlyfans-ui/build.mjs
    node scripts/check-onlyfans-ui-browser.mjs
  ' > "$project/evidence/continuation-browser-regressions.log" 2>&1
