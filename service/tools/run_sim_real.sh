#!/usr/bin/env bash
# One real-model simulation run: throwaway Postgres + engine container on a private bridge network.
# Network egress is needed ONLY for openrouter.ai; the fan side stays synthetic. Key file is mounted read-only, never printed.
set -u
P=/srv/projects/twin-engine-completion-20261107/a6sim
KEY=/home/worker/.config/twin-engine/openrouter.key
NET=sim-real-net-20261107; PG=sim-real-pg-20261107
docker ps --format '{{.Names}}' | grep -q '^twin-engine-offline' && { echo "another offline run active"; exit 9; }
mkdir -p $P/sim-out
docker network create $NET >/dev/null
docker run -d --rm --name $PG --network $NET --tmpfs /var/lib/postgresql/data:rw,size=1500m \
  -e POSTGRES_USER=twin_lab -e POSTGRES_DB=twin_engine_lab -e POSTGRES_HOST_AUTH_METHOD=trust postgres:16 >/dev/null
for i in $(seq 60); do docker exec $PG pg_isready -U twin_lab >/dev/null 2>&1 && break; sleep 1; done
docker run --rm --name twin-engine-offline-simreal --pull never --cpus 0.75 --cap-drop ALL --read-only --memory 1024m \
  --security-opt no-new-privileges --user 1000:1000 --tmpfs /tmp:rw,nosuid,size=128m -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 \
  -v /srv/projects/twin-conversation-engine-20261107/test-packages:/opt/test-packages:ro \
  -v $P:/app:ro -v $P/sim-out:/out -v $KEY:$KEY:ro \
  --network $NET \
  -e PYTHONPATH=/opt/test-packages:/app/service:/app/service/tests -e PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  -e ONLYFANS_TEST_PG_URL=postgresql://twin_lab@$PG:5432/postgres -e ONLYFANS_TEST_PG_EMPTY_TEMPLATE=1 \
  -e ENGINE_MODEL_ENABLED=1 -e ENGINE_MODEL_ACCOUNTS=acc-a \
  -e ENGINE_MODEL_ALLOWED=deepseek/deepseek-v4.1-flash -e ENGINE_MODEL_PRIMARY=deepseek/deepseek-v4.1-flash \
  -e ENGINE_MODEL_ALTERNATE=deepseek/deepseek-v4.1-flash -e ENGINE_MODEL_PROMPT_PROVIDER=a-original \
  -e ENGINE_MODEL_KEY_FILE=$KEY -e ENGINE_MODEL_DAILY_CAP_MICROUSD=2000000 -e ENGINE_MODEL_CREATOR_CAP_MICROUSD=2000000 \
  -w /app/service --entrypoint /usr/local/bin/python twin-engine-offline-main:20261107-r6 -m tools.sim_journey "$@"
rc=$?
docker stop $PG >/dev/null 2>&1; docker network rm $NET >/dev/null 2>&1
exit $rc
