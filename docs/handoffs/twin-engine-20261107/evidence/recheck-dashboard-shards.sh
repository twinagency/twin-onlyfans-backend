#!/usr/bin/env bash
set -euo pipefail
p=/srv/projects/twin-conversation-engine-20261107
[[ "$(hostname)" == ubuntu-s-4vcpu-8gb-syd1 ]]
for part in 03 08; do
  docker run --rm --pull never --cpus 0.75 --pids-limit 96 --cap-drop ALL --security-opt no-new-privileges --read-only --user 1000:1000 --network none --memory 1536m --memory-swap 1536m --tmpfs /tmp:rw,nosuid,size=128m -e HOME=/tmp -e NODE_OPTIONS=--max-old-space-size=1200 -v "$p/dashboard:/workspace:ro" -v "$p/evidence:/evidence:ro" -v /usr/local/bin/node:/usr/local/bin/node:ro -w /workspace --entrypoint /usr/local/bin/node twin-engine-offline-main:20261107-dev7 node_modules/typescript/bin/tsc -p "/evidence/tsc-shards-continuation/part-$part.json" --extendedDiagnostics > "$p/evidence/tsc-shards-continuation/part-$part-rerun.log" 2>&1
  echo "part-$part corrected rerun passed"
done
