#!/usr/bin/env bash
set -euo pipefail
project=/srv/projects/twin-conversation-engine-20261107
[ "$(hostname)" = ubuntu-s-4vcpu-8gb-syd1 ]
[ -d "$project/dashboard/.git" ]
docker run --rm --pull never --cpus 0.75 --pids-limit 96 --cap-drop ALL --security-opt no-new-privileges --read-only --user 1000:1000 --network none --memory 1536m --memory-swap 1536m --tmpfs /tmp:rw,nosuid,size=128m -e HOME=/tmp -e NODE_OPTIONS=--max-old-space-size=1200 -e TSC_SHARD_SIZE=150 -e TSC_SHARD_OUTPUT=/evidence/tsc-shards-continuation -v "$project/dashboard:/workspace:ro" -v "$project/evidence:/evidence" -v /usr/local/bin/node:/usr/local/bin/node:ro -w /workspace --entrypoint /bin/sh twin-engine-offline-main:20261107-dev7 -c '
  set -u
  node /evidence/build-tsc-shards.cjs || exit 1
  result=0
  for config in /evidence/tsc-shards-continuation/part-*.json; do
    name=$(basename "$config" .json)
    echo "Checking $name"
    if node node_modules/typescript/bin/tsc -p "$config" --extendedDiagnostics > "/evidence/tsc-shards-continuation/$name.log" 2>&1; then
      echo "$name passed"
    else
      echo "$name failed"
      result=1
    fi
  done
  exit "$result"
' > "$project/evidence/tsc-shards-continuation-summary.log" 2>&1
