#!/usr/bin/env bash
# Synthetic tests only. No published ports, external network, or live credentials.
set -euo pipefail
project=/srv/projects/twin-conversation-engine-20261107
image=moxy-current-base:verified-e5f092544145
pg=twin-engine-lab-pg-capped-20261107
[ "$(hostname)" = ubuntu-s-4vcpu-8gb-syd1 ]
[ -d "$project/backend/.git" ] && [ -d "$project/dashboard/.git" ]
common=(--rm --pull never --cpus 0.75 --pids-limit 96 --cap-drop ALL --security-opt no-new-privileges --read-only --user "$(id -u):$(id -g)" -e HOME=/tmp)
docker run "${common[@]}" --network none --memory 768m --tmpfs /tmp:rw,noexec,nosuid,size=64m -e PYTHONPATH=/workspace/service -e PYTHONDONTWRITEBYTECODE=1 -v "$project/backend:/workspace:ro" -w /workspace --entrypoint /usr/local/bin/python "$image" -m unittest discover -s service/tests/conversation_engine -p 'test_*.py' -v
docker run "${common[@]}" --network none --memory 768m --tmpfs /tmp:rw,noexec,nosuid,size=64m -v "$project/dashboard:/workspace:ro" -v /usr/local/bin/node:/usr/local/bin/node:ro -w /workspace --entrypoint /usr/local/bin/node "$image" --experimental-strip-types --experimental-loader ./tests/contracts/resolve-typescript.mjs --test tests/contracts/facade.test.mjs
if [ "${1:-}" = "--postgres" ]; then
  [ "$(docker inspect "$pg" --format '{{.HostConfig.NetworkMode}}')" = none ]
  [ "$(docker inspect "$pg" --format '{{.State.Running}}')" = true ] || { echo 'Start the dedicated lab container first.' >&2; exit 1; }
  docker exec "$pg" pg_isready -U twin_lab -d twin_engine_lab >/dev/null
  docker run "${common[@]}" --network "container:$pg" --memory 1024m --tmpfs /tmp:rw,nosuid,size=128m -e PYTHONPATH=/opt/test-packages:/workspace/service:/workspace/service/tests -e PYTHONDONTWRITEBYTECODE=1 -e PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 -e ONLYFANS_TEST_PG_URL=postgresql://twin_lab@127.0.0.1:5432/postgres -v "$project/backend:/workspace:ro" -v "$project/test-packages:/opt/test-packages:ro" -w /workspace --entrypoint /usr/local/bin/python "$image" -m pytest -q -p no:cacheprovider service/tests/test_onlyfans_foundation_pg.py::test_populated_baseline_upgrade_preserves_evidence service/tests/test_onlyfans_text_delivery_pg.py::test_projection_crash_is_repaired_without_a_second_wire_call
fi
