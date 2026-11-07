#!/usr/bin/env bash
# usage: runsim.sh <args to python -m ...>  (runs in offline image against sim-pg-20261107)
P=/srv/projects/twin-engine-completion-20261107/sim
docker ps --format '{{.Names}}' | grep -q '^twin-engine-offline' && { echo "another offline run active"; exit 9; }
docker run --rm --name twin-engine-offline-sim --pull never --cpus 0.75 --cap-drop ALL --read-only --memory 1024m \
  --security-opt no-new-privileges --user 1000:1000 --tmpfs /tmp:rw,nosuid,size=128m -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 \
  -v /srv/projects/twin-conversation-engine-20261107/test-packages:/opt/test-packages:ro \
  -v $P:/app:ro -v $P/sim-out:/out \
  --network container:sim-pg-20261107 \
  -e PYTHONPATH=/opt/test-packages:/app/service:/app/service/tests -e PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  -e ONLYFANS_TEST_PG_URL=postgresql://twin_lab@127.0.0.1:5432/postgres -e ONLYFANS_TEST_PG_EMPTY_TEMPLATE=1 \
  -w /app --entrypoint /usr/local/bin/python twin-engine-offline-main:20261107-r6 "$@"
