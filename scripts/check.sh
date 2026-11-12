#!/usr/bin/env bash
# Single-entry test harness (FIX-MODE F0). Merge gate = `scripts/check.sh fast` GREEN.
#   fast  premerge.sh, then the live smoke when the local stack is up (skipped with a note when down).
#         HARD wall budget CHECK_BUDGET_S (900 = 15 min) at nice 10; exceeding it is RED.
#         premerge.sh step (c) already runs every test in scripts/premerge-promoted.txt: not run twice here.
#   live  read-only smoke of the local stack (scripts/check_live.py, GET only, loopback only, <3 min).
#   full  whole suite: run-service-tests.py (--postgres when ONLYFANS_TEST_PG_URL is set) + app vitest. No budget.
# Exit 0 GREEN, 1 RED, 2 INFRA (environment missing: premerge INCOMPLETE e.g. no PG URL, stack down in live mode).
# Evidence: .check-evidence/<UTC-ts>-<shortsha>/ (step logs, timings.txt, verdict.txt). Tokens are never logged.
# env: PY, ONLYFANS_TEST_PG_URL, CHECK_BUDGET_S, CHECK_PREMERGE_CMD (test seam), CHECK_EVIDENCE_ROOT, and
#      check_live.py's CHECK_API, CHECK_DASH, CHECK_SIGNIN, CHECK_DB_URL, CHECK_ENV_FILE, CHECK_TOKEN_VAR.
set -uo pipefail
[ -z "${CHECK_NICED:-}" ] && exec env CHECK_NICED=1 nice -n 10 bash "$0" "$@"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT"
mode="${1:-}"; case "$mode" in fast|live|full) ;; *) echo "usage: scripts/check.sh fast|live|full" >&2; exit 64;; esac
PY="${PY:-$( [ -x .venv/bin/python ] && echo "$ROOT/.venv/bin/python" || echo "$ROOT/venv/bin/python" )}"; export PY
TO="$(command -v timeout || command -v gtimeout || true)"
EV="${CHECK_EVIDENCE_ROOT:-$ROOT/.check-evidence}/$(date -u +%Y%m%dT%H%M%SZ)-$(git rev-parse --short HEAD 2>/dev/null || echo nogit)"
mkdir -p "$EV"; T0=$SECONDS; red=0 infra=0 notes=()
echo "check $mode: evidence $EV"

# step NAME LIMIT_S CMD...: output teed to $EV/NAME.log; LIMIT_S>0 kills the process group past it (rc 124).
step() { local name=$1 lim=$2 s=$SECONDS rc; shift 2
  if [ "$lim" -gt 0 ] && [ -n "$TO" ]; then set -- "$TO" -k 5 "$lim" "$@"; fi
  "$@" </dev/null 2>&1 | tee "$EV/$name.log"; rc=${PIPESTATUS[0]}
  echo "$name rc=$rc $((SECONDS-s))s" >>"$EV/timings.txt"; return "$rc"; }
note() { notes+=("$1"); }

live_rc() {  # map check_live.py's exit code; $1 = verdict for "stack down"
  case $2 in 0) ;; 2) [ "$1" = skip ] && note "live skipped: local stack down" || { infra=1; note "live: stack down"; };;
    124|137) red=1; note "live killed: budget exceeded";; *) red=1; note "live RED rc=$2";; esac; }

case "$mode" in
fast)
  budget="${CHECK_BUDGET_S:-900}"
  step premerge "$budget" bash -c "${CHECK_PREMERGE_CMD:-scripts/premerge.sh}"; rc=$?
  case $rc in 0) ;; 2) infra=1; note "premerge INCOMPLETE (tests skipped, e.g. no ONLYFANS_TEST_PG_URL)";;
    124|137) red=1; note "premerge killed: budget ${budget}s exceeded";; *) red=1; note "premerge FAILED rc=$rc";; esac
  left=$((budget - (SECONDS - T0)))
  if [ "$left" -gt 0 ]; then step live "$left" "$PY" scripts/check_live.py; live_rc skip $?; fi
  [ $((SECONDS - T0)) -ge "$budget" ] && { red=1; note "wall $((SECONDS - T0))s >= budget ${budget}s"; } ;;
live)
  step live 0 "$PY" scripts/check_live.py; live_rc infra $? ;;
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
