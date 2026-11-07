#!/usr/bin/env bash
# Offline test artifacts only; no service activation or production data.
set -euo pipefail
project=/srv/projects/twin-conversation-engine-20261107
pg=twin-engine-lab-pg-capped-20261107
db=twin_offline_qualification_0118_20261107_r3
main=twin-engine-offline-main:20261107-r3
generic=twin-engine-offline-generic:20261107-r3
[[ "$(hostname)" == ubuntu-s-4vcpu-8gb-syd1 ]]
[[ "$(docker inspect "$pg" --format '{{.HostConfig.NetworkMode}}')" == none ]]
[[ "$(docker inspect "$pg" --format '{{.State.Running}}')" == true ]]
[[ -z "$(git -C "$project/backend" status --porcelain)" ]]
commit=$(git -C "$project/backend" rev-parse HEAD)
cd "$project/backend"
DOCKER_BUILDKIT=0 docker build --network none --cpu-quota 75000 --memory 1024m --memory-swap 1024m --target main --build-arg "SOURCE_COMMIT=$commit" -f ../evidence/Dockerfile.offline -t "$main" . > "$project/evidence/continuation-main-image-build.log" 2>&1
DOCKER_BUILDKIT=0 docker build --network none --cpu-quota 75000 --memory 1024m --memory-swap 1024m --target generic-vault --build-arg "SOURCE_COMMIT=$commit" -f ../evidence/Dockerfile.offline -t "$generic" . > "$project/evidence/continuation-generic-image-build.log" 2>&1
docker image inspect "$main" --format '{{.Id}}' > "$project/evidence/continuation-main-image-id.txt"
docker image inspect "$generic" --format '{{.Id}}' > "$project/evidence/continuation-generic-image-id.txt"
[[ -z "$(docker exec "$pg" psql -U twin_lab -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname = '$db'")" ]]
docker exec "$pg" createdb -U twin_lab "$db"
common=(--rm --pull never --cpus 0.75 --pids-limit 96 --cap-drop ALL --security-opt no-new-privileges --read-only --user 1000:1000 --network "container:$pg" --memory 1024m --tmpfs /tmp:rw,nosuid,size=128m -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1)
url="postgresql://twin_lab@127.0.0.1:5432/$db"
{
  docker run "${common[@]}" -e ONLYFANS_MIGRATION_URL="$url" --entrypoint /usr/local/bin/python "$main" -m db.schema bootstrap
  docker run "${common[@]}" -e ONLYFANS_MIGRATION_URL="$url" --entrypoint /usr/local/bin/python "$main" -m db.schema check
  docker run "${common[@]}" -e ONLYFANS_MIGRATION_URL="$url" --entrypoint /usr/local/bin/python "$generic" -m db.schema check
  docker run "${common[@]}" --entrypoint /usr/local/bin/python "$main" -c 'from db.schema import HEAD; import vault_ai_api; print("main schema head:", HEAD, vault_ai_api.__file__)'
  docker run "${common[@]}" --entrypoint /usr/local/bin/python "$generic" -c 'from db.schema import HEAD; import vault_ai_api; print("generic schema head:", HEAD, vault_ai_api.__file__)'
} > "$project/evidence/continuation-image-schema-checks.log" 2>&1
for image in "$main" "$generic"; do
  label=main; [[ "$image" == "$generic" ]] && label=generic
  docker run "${common[@]}" -e PYTHONPATH=/opt/test-packages:/app/service:/app/service/tests -e PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
    -e ONLYFANS_TEST_PG_EMPTY_TEMPLATE=1 -e ONLYFANS_TEST_PG_URL=postgresql://twin_lab@127.0.0.1:5432/postgres \
    -v "$project/test-packages:/opt/test-packages:ro" -w /app --entrypoint /usr/local/bin/python "$image" \
    -m pytest -q --disable-warnings --maxfail=1 -p no:cacheprovider \
    service/tests/test_engine_simulated_journey_pg.py::test_incoming_to_decision_single_authority_receipt_and_dashboard_projection \
    service/tests/test_fans_local_http_pg.py::test_restart_recovers_from_durable_authenticated_webhook_without_redelivery \
    service/tests/test_fans_local_http_pg.py::test_restart_maintenance_preserves_expired_wire_as_unknown \
    > "$project/evidence/continuation-$label-image-journeys.log" 2>&1
done
docker run "${common[@]}" -e PYTHONPATH=/opt/test-packages:/app/service:/app/service/tests -e PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  -e ONLYFANS_TEST_PG_EMPTY_TEMPLATE=1 -e ONLYFANS_TEST_PG_URL=postgresql://twin_lab@127.0.0.1:5432/postgres \
  -v "$project/test-packages:/opt/test-packages:ro" -v "$project/evidence:/evidence:ro" -w /app --entrypoint /usr/local/bin/python "$generic" \
  -m pytest -q --disable-warnings --maxfail=1 -p no:cacheprovider /evidence/test_packaged_generic_vault.py \
  > "$project/evidence/continuation-generic-vault-tests.log" 2>&1
