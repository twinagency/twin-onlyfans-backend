#!/usr/bin/env bash
# Container wrapper for the Plan C simulation (same sandbox as ~worker/pilotrun.sh, plus one writable output folder).
#
#   OUT_DIR=/abs/output/dir  [PILOT_TREE=<worktree>]  run_plan_c_sim.sh --dry-run --model m/primary --fallback m/fallback
#   OUT_DIR=...  OPENROUTER_API_KEY=...  run_plan_c_sim.sh --live --model <id> --price-in <usd/Mtok> --price-out <usd/Mtok> ...
#
# The tree is mounted read-only; results go to OUT_DIR (the tool is given --out-dir /out). --live reads the key from the
# environment variable named by --key-env (default OPENROUTER_API_KEY); the key is passed into the container by NAME
# only and is never printed. The FansAPI side is ALWAYS the fake loopback server. Do not pass --out-dir yourself.
set -euo pipefail
: "${OUT_DIR:?set OUT_DIR to an absolute directory that user 1000 can write}"
mkdir -p "$OUT_DIR"
TREE="${PILOT_TREE:-/srv/projects/twin-engine-completion-20261107/planc-sim}"
for n in 1 2 3; do exec 9>/tmp/pilotrun.$((n%2)).lock; flock -n 9 && break; sleep 3; done; flock 9
PLAN_C_SIM_COMMIT="${PLAN_C_SIM_COMMIT:-$(git -C "$TREE" rev-parse HEAD 2>/dev/null || true)}"
KEYARG=()
if [ -n "${OPENROUTER_API_KEY:-}" ]; then KEYARG=(-e OPENROUTER_API_KEY); fi
exec docker run --rm --name "plan-c-sim-$$" --pull never --cpus 0.75 --pids-limit 96 --cap-drop ALL \
  --security-opt no-new-privileges --read-only --user 1000:1000 \
  --network container:pilot-pg-20261107 --memory 1024m --tmpfs /tmp:rw,nosuid,size=128m \
  -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONPATH=/opt/test-packages:/app/service:/app/service/tests \
  -e PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 -e ONLYFANS_TEST_PG_EMPTY_TEMPLATE=1 \
  -e ONLYFANS_TEST_PG_URL=postgresql://twin_lab@127.0.0.1:5432/postgres \
  -e PLAN_C_SIM_COMMIT="${PLAN_C_SIM_COMMIT:-}" "${KEYARG[@]}" \
  -v /srv/projects/twin-conversation-engine-20261107/test-packages:/opt/test-packages:ro \
  -v "$TREE":/app:ro -v "$OUT_DIR":/out:rw \
  -w /app --entrypoint /usr/local/bin/python twin-engine-offline-main:20261107-r6 \
  -m tools.plan_c_sim "$@" --out-dir /out
