#!/usr/bin/env bash
# R0 host snapshot (final implementation plan, 1 Nov 2026). Operator only.
# READ ONLY: lists containers, their state and immutable image identities, and
# hashes the compose/release manifests. It does not start, stop, pull, build or
# recreate anything. The supervisor is expected to be intentionally STOPPED.
set -uo pipefail

echo "== checked_at: $(date -u +%FT%TZ)"
echo "== containers (all, including stopped)"
docker ps -a --format '{{.Names}}\t{{.State}}\t{{.Status}}\t{{.Image}}' | sort

echo "== image identity per container"
for c in $(docker ps -a --format '{{.Names}}' | sort); do
  img_id=$(docker inspect -f '{{.Image}}' "$c" 2>/dev/null)
  repo_digests=$(docker image inspect -f '{{join .RepoDigests " "}}' "$img_id" 2>/dev/null)
  created=$(docker inspect -f '{{.Created}}' "$c" 2>/dev/null)
  started=$(docker inspect -f '{{.State.StartedAt}}' "$c" 2>/dev/null)
  restarts=$(docker inspect -f '{{.RestartCount}}' "$c" 2>/dev/null)
  labels=$(docker inspect -f '{{range $k,$v := .Config.Labels}}{{$k}}={{$v}} {{end}}' "$c" 2>/dev/null | tr ' ' '\n' | grep -iE 'revision|source|version|commit' | tr '\n' ' ')
  printf '%s\timage=%s\tdigests=%s\tcreated=%s\tstarted=%s\trestarts=%s\t%s\n' \
    "$c" "$img_id" "${repo_digests:-none}" "$created" "$started" "$restarts" "$labels"
done

echo "== images tagged twin-repair / twin-onlyfans (newest first)"
docker images --format '{{.CreatedAt}}\t{{.Repository}}:{{.Tag}}\t{{.ID}}' | grep -E 'twin-repair|twin-onlyfans' | sort -r | head -20

echo "== release/compose manifests (sha256 only, contents not printed)"
for f in /etc/twin-onlyfans/*.json /etc/twin-onlyfans/*.yml /etc/twin-onlyfans/*.yaml; do
  [ -f "$f" ] && sha256sum "$f"
done
ls -1dt /var/lib/twin-release/* 2>/dev/null | head -10

echo "== PR #76 install trace: any container or image mentioning conversation-integrity"
docker ps -a --format '{{.Names}}\t{{.Image}}\t{{.State}}' | grep -i 'conversation-integrity' || echo "no container on that image"
docker images | grep -i 'conversation-integrity' || echo "no image with that tag"
