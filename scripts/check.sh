#!/usr/bin/env bash
# Test harness (FIX-MODE F0); merge gate = `scripts/check.sh fast` GREEN. Exit 0 GREEN, 1 RED, 2 INFRA (env missing).
#   fast  PR gate: premerge.sh lint + changed tests + promoted + touched-area, then live unless stack is down.
#         HARD budget CHECK_BUDGET_S=900 at nice 10: over = RED; bad budget or no timeout = INFRA, nothing run.
#         Split (F0d): touched-area tests not started by budget-300 s, or still running at budget-200 s (budget/2, 3/4
#         if budget <= 600), are DEFERRED: listed in the output + $EV/deferred.txt and noted in the verdict. Changed +
#         promoted tests are never deferred (they can still blow the budget = RED). Verdict then reads GREEN (N deferred).
#   wide  same full set as fast, nothing deferred, no budget. The orchestrator runs it before every stack restart.
#   live  scripts/check_live.py (read-only; see its docstring), 180 s cap.   full  run-service-tests + vitest.
# No timeout/gtimeout = INFRA in every mode, nothing run. INT/TERM kills the running step's process group.
# Evidence .check-evidence/<UTC-ts>-<shortsha>/. env: PY ONLYFANS_TEST_PG_URL ALLOW_SKIP CHECK_BUDGET_S CHECK_EVIDENCE_ROOT CHECK_JOBS
set -uo pipefail; umask 077  # evidence holds stack output: owner-only
[ -z "${CHECK_NICED:-}" ] && exec env CHECK_NICED=1 nice -n 10 bash "$0" "$@"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT"
mode="${1:-}"; case "$mode" in fast|wide|live|full) ;; *) echo "usage: scripts/check.sh fast|wide|live|full" >&2; exit 64;; esac
PY="${PY:-$( [ -x .venv/bin/python ] && echo "$ROOT/.venv/bin/python" || echo "$ROOT/venv/bin/python" )}"; export PY
TO="$(command -v timeout || command -v gtimeout || true)"
EV="${CHECK_EVIDENCE_ROOT:-$ROOT/.check-evidence}/$(date -u +%Y%m%dT%H%M%SZ)-$(git rev-parse --short HEAD 2>/dev/null || echo nogit)"
mkdir -p "$EV" || { echo "check $mode: INFRA (evidence dir not writable: $EV)" >&2; exit 2; }; T0=$SECONDS; red=0 infra=0 notes=()
echo "check $mode: evidence $EV"; ndef=0
trap 'p=$(cat "$EV/.pid" 2>/dev/null) && { kill -TERM -- "-$p" 2>/dev/null || kill -TERM "$p"; }; exit 130' INT TERM

# step NAME LIMIT_S CMD: tee to $EV/NAME.log; LIMIT_S>0 = timeout -k cap (rc 124). Its pid goes to $EV/.pid for the trap.
step() { local name=$1 lim=$2 s=$SECONDS rc; shift 2
  [ "$lim" -gt 0 ] && set -- "$TO" -k 5 "$lim" "$@"
  { "$@" </dev/null & echo $! >"$EV/.pid"; wait $!; } 2>&1 | tee "$EV/$name.log"; rc=${PIPESTATUS[0]}; rm -f "$EV/.pid"
  echo "$name rc=$rc $((SECONDS-s))s" >>"$EV/timings.txt"; return "$rc"; }
note() { notes+=("$1"); }
live() {  # $1 = cap seconds, $2 = what "whole stack down" means (skip | infra); SKIP lines become notes
  step live "$1" "$PY" scripts/check_live.py; local rc=$? l
  while IFS= read -r l; do note "live $l"; done < <(grep -s '^SKIP' "$EV/live.log")
  case $rc in 0) ;; 2) infra=1;; 3) [ "$2" = skip ] && note "live skipped: local stack down" || { infra=1; note "live: stack down"; };;
    124|137) red=1; note "live killed: time cap";; *) red=1; note "live RED rc=$rc";; esac; }
premerge_rc() { case $1 in 0) ;; 2) infra=1; note "premerge INCOMPLETE (tests skipped, e.g. no ONLYFANS_TEST_PG_URL)";;
    124|137) red=1; note "premerge killed: budget ${budget}s exceeded";; *) red=1; note "premerge FAILED rc=$1";; esac
  grep -s '^DEFERRED to wide: ' "$EV/premerge.log" >"$EV/deferred.txt"; local d; d=$(wc -l <"$EV/deferred.txt" | tr -d ' ')
  ndef=$d; [ "$d" = 0 ] || note "deferred to wide: $d touched-area tests not run (list: $EV/deferred.txt); run scripts/check.sh wide"; }
[ -n "$TO" ] || { infra=1; note "no timeout/gtimeout (coreutils): nothing run"; }
[ "$infra" = 1 ] || case "$mode" in
fast)
  budget="${CHECK_BUDGET_S:-900}"
  if ! [[ $budget =~ ^[1-9][0-9]{0,4}$ ]]; then infra=1; note "CHECK_BUDGET_S=$budget not a positive int <= 99999: nothing run"
  else step premerge "$budget" env PREMERGE_DEFER_AFTER_S=$((budget > 600 ? budget - 300 : budget / 2)) \
    PREMERGE_STOP_AT_S=$((budget > 600 ? budget - 200 : budget * 3 / 4)) scripts/premerge.sh
  premerge_rc $?
  left=$((budget - (SECONDS - T0))); [ "$left" -gt 0 ] && live $((left < 180 ? left : 180)) skip
  [ $((SECONDS - T0)) -ge "$budget" ] && { red=1; note "wall $((SECONDS - T0))s >= budget ${budget}s"; }; fi ;;
wide) budget=none; step premerge 0 env -u PREMERGE_DEFER_AFTER_S -u PREMERGE_STOP_AT_S scripts/premerge.sh; premerge_rc $?; live 180 skip ;;
live) live 180 infra ;;
full)
  args=(); if [ -n "${ONLYFANS_TEST_PG_URL:-}" ]; then args=(--postgres); else infra=1; note "no ONLYFANS_TEST_PG_URL: *_pg.py modules not run"; fi
  step service 0 "$PY" scripts/run-service-tests.py ${args[@]+"${args[@]}"} || { red=1; note "service tests FAILED"; }
  if [ -d app/node_modules/vitest ]; then step vitest 0 bash -c 'cd app && pnpm test:run' || { red=1; note "vitest FAILED"; }
  else infra=1; note "app vitest not installed: frontend not run"; fi ;;
esac

v=GREEN rc=0; [ "$ndef" = 0 ] || v="GREEN ($ndef deferred)"; [ "$infra" = 1 ] && v=INFRA rc=2; [ "$red" = 1 ] && v=RED rc=1
printf '%s\n' "$v" ${notes[@]+"${notes[@]}"} "wall $((SECONDS - T0))s" >"$EV/verdict.txt" || { [ $rc = 1 ] || v=INFRA rc=2; }
echo "check $mode: $v (wall $((SECONDS - T0))s) ${notes[*]+${notes[*]}}"; echo "evidence: $EV"
exit "$rc"
