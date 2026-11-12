#!/usr/bin/env bash
# Test harness (FIX-MODE F0); merge gate = `scripts/check.sh fast` GREEN. Exit 0 GREEN, 1 RED, 2 INFRA (env missing).
#   fast  premerge.sh (its step (c) runs scripts/premerge-promoted.txt; not repeated), then live unless stack is down.
#         HARD budget CHECK_BUDGET_S=900 at nice 10: over = RED; bad budget or no timeout = INFRA, nothing run.
#   live  scripts/check_live.py (read-only; see its docstring), 180 s cap.   full  run-service-tests + vitest.
# Evidence .check-evidence/<UTC-ts>-<shortsha>/. env: PY ONLYFANS_TEST_PG_URL ALLOW_SKIP CHECK_BUDGET_S CHECK_EVIDENCE_ROOT
set -uo pipefail
[ -z "${CHECK_NICED:-}" ] && exec env CHECK_NICED=1 nice -n 10 bash "$0" "$@"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT"
mode="${1:-}"; case "$mode" in fast|live|full) ;; *) echo "usage: scripts/check.sh fast|live|full" >&2; exit 64;; esac
PY="${PY:-$( [ -x .venv/bin/python ] && echo "$ROOT/.venv/bin/python" || echo "$ROOT/venv/bin/python" )}"; export PY
TO="$(command -v timeout || command -v gtimeout || true)"
EV="${CHECK_EVIDENCE_ROOT:-$ROOT/.check-evidence}/$(date -u +%Y%m%dT%H%M%SZ)-$(git rev-parse --short HEAD 2>/dev/null || echo nogit)"
mkdir -p "$EV"; T0=$SECONDS; red=0 infra=0 notes=()
echo "check $mode: evidence $EV"

# step NAME LIMIT_S CMD: tee to $EV/NAME.log; LIMIT_S>0 = timeout -k cap (rc 124), no timeout binary = INFRA, not run.
step() { local name=$1 lim=$2 s=$SECONDS rc; shift 2
  [ "$lim" -gt 0 ] && [ -z "$TO" ] && { infra=1; note "no timeout/gtimeout: $name not run"; return 0; }
  [ "$lim" -gt 0 ] && set -- "$TO" -k 5 "$lim" "$@"
  "$@" </dev/null 2>&1 | tee "$EV/$name.log"; rc=${PIPESTATUS[0]}
  echo "$name rc=$rc $((SECONDS-s))s" >>"$EV/timings.txt"; return "$rc"; }
note() { notes+=("$1"); }
live() {  # $1 = cap seconds, $2 = what "whole stack down" means (skip | infra); SKIP lines become notes
  step live "$1" "$PY" scripts/check_live.py; local rc=$? l
  while IFS= read -r l; do note "live $l"; done < <(grep -s '^SKIP' "$EV/live.log")
  case $rc in 0) ;; 2) infra=1;; 3) [ "$2" = skip ] && note "live skipped: local stack down" || { infra=1; note "live: stack down"; };;
    124|137) red=1; note "live killed: time cap";; *) red=1; note "live RED rc=$rc";; esac; }
case "$mode" in
fast)
  budget="${CHECK_BUDGET_S:-900}"
  if ! [[ $budget =~ ^[1-9][0-9]*$ ]]; then infra=1; note "CHECK_BUDGET_S=$budget not a positive int: nothing run"
  else step premerge "$budget" scripts/premerge.sh; rc=$?
  case $rc in 0) ;; 2) infra=1; note "premerge INCOMPLETE (tests skipped, e.g. no ONLYFANS_TEST_PG_URL)";;
    124|137) red=1; note "premerge killed: budget ${budget}s exceeded";; *) red=1; note "premerge FAILED rc=$rc";; esac
  left=$((budget - (SECONDS - T0))); [ "$left" -gt 0 ] && live $((left < 180 ? left : 180)) skip
  [ $((SECONDS - T0)) -ge "$budget" ] && { red=1; note "wall $((SECONDS - T0))s >= budget ${budget}s"; }; fi ;;
live) live 180 infra ;;
full)
  args=(); if [ -n "${ONLYFANS_TEST_PG_URL:-}" ]; then args=(--postgres); else infra=1; note "no ONLYFANS_TEST_PG_URL: *_pg.py modules not run"; fi
  step service 0 "$PY" scripts/run-service-tests.py ${args[@]+"${args[@]}"} || { red=1; note "service tests FAILED"; }
  if [ -d app/node_modules/vitest ]; then step vitest 0 bash -c 'cd app && pnpm test:run' || { red=1; note "vitest FAILED"; }
  else infra=1; note "app vitest not installed: frontend not run"; fi ;;
esac

v=GREEN rc=0; [ "$infra" = 1 ] && v=INFRA rc=2; [ "$red" = 1 ] && v=RED rc=1
{ echo "$v"; [ ${#notes[@]} -gt 0 ] && printf '%s\n' "${notes[@]}"; echo "wall $((SECONDS - T0))s"; } >"$EV/verdict.txt"
echo "check $mode: $v (wall $((SECONDS - T0))s) ${notes[*]+${notes[*]}}"; echo "evidence: $EV"
exit "$rc"
