#!/usr/bin/env bash
# FIX-MODE pipeline gate (added in the first fix PR; CI is billing-locked): every fix PR runs it.
#   scripts/premerge.sh [extra test files...]   env: BASE PY ONLYFANS_TEST_PG_URL ALLOW_SKIP=1 CHECK_JOBS CHECK_FILE_TIMEOUT_S
#                                                    PREMERGE_DEFER_AFTER_S PREMERGE_STOP_AT_S (both set by check.sh fast)
# (a) lint python changed since merge-base($BASE) (syntax + undefined names)
# (b) pytest: changed service/tests/**/test_*.py + extra args        (c) scripts/premerge-promoted.txt (path[::test]/line)
# (d) touched-area: tests that mention a changed module. (d) targets not started by PREMERGE_DEFER_AFTER_S, or cut at
#     PREMERGE_STOP_AT_S (s since start; only when that cut fired), print "DEFERRED to wide: <t>"; (b)+(c) are never
#     deferred. Targets (deduped, first section wins) run CHECK_JOBS (3) at a time, each its own process, output printed
#     in list order; files in scripts/premerge-serial.txt (contention-flaky) run alone. TERM/INT reaps running targets.
# Exit 0 = OK, 1 = FAILED, 2 = INCOMPLETE (something skipped, ALLOW_SKIP=1 accepts; or no timeout binary). Pre-existing
# reds live in scripts/premerge-known-red.txt (proven red at base): pytest ids are deselected (a file whose every test
# is listed passes only on pytest's exact "N deselected in Xs", rc 5); a script must end on the listed line, digit runs
# written as # (volatile timestamps/ids) - only for scripts that abort on their first failure. Any other failure, or a
# listed script that passes, FAILS. Network blocked via dead proxy. Each target is capped at CHECK_FILE_TIMEOUT_S (600):
# over = FAIL; a killed target (rc 137 before its cap) = FAIL.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT"
BASE="${BASE:-origin/claude/commercial-r0-r3-20261111}"
PY="${PY:-$( [ -x .venv/bin/python ] && echo "$ROOT/.venv/bin/python" || echo "$ROOT/venv/bin/python" )}"
PROMOTED=scripts/premerge-promoted.txt KNOWN=scripts/premerge-known-red.txt
export PYTHONPATH="$ROOT:$ROOT/service:$ROOT/service/tests:$ROOT/ops/plan-c-supervision${PYTHONPATH:+:$PYTHONPATH}"
export HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 http_proxy=http://127.0.0.1:9 https_proxy=http://127.0.0.1:9 NO_PROXY= no_proxy=
git rev-parse --verify --quiet "$BASE^{commit}" >/dev/null || { echo "premerge: FAILED (unknown BASE $BASE)"; exit 1; }
[ -f "$PROMOTED" ] || { echo "premerge: FAILED (missing $PROMOTED)"; exit 1; }
MB="$(git merge-base "$BASE" HEAD)"
JOBS="${CHECK_JOBS:-3}"; [[ $JOBS =~ ^[1-9][0-9]?$ ]] || { echo "premerge: FAILED (CHECK_JOBS=$JOBS not 1..99)"; exit 1; }
FTO="${CHECK_FILE_TIMEOUT_S:-600}"; [[ $FTO =~ ^[1-9][0-9]{0,4}$ ]] || { echo "premerge: FAILED (CHECK_FILE_TIMEOUT_S=$FTO)"; exit 1; }
TO="$(command -v timeout || command -v gtimeout || true)"
[ -n "$TO" ] || { echo "premerge: INCOMPLETE (no timeout/gtimeout for the per-file cap: INFRA)"; exit 2; }
[[ ${PREMERGE_DEFER_AFTER_S:-0}${PREMERGE_STOP_AT_S:-0} =~ ^[0-9]+$ ]] || { echo "premerge: FAILED (PREMERGE_*_S not ints)"; exit 1; }
list="$(mktemp)" out="$(mktemp -d)"
reap() { local p; for p in "$out"/*.pid; do [ -f "$p" ] && kill -TERM -- "-$(cat "$p")" 2>/dev/null; done; return 0; }
trap 'reap; rm -rf "$list" "$out"' EXIT; trap 'exit 143' TERM HUP; trap 'exit 130' INT  # inner timeouts lead own pgroups
git diff --name-only --diff-filter=d "$MB" >"$list"; git ls-files --others --exclude-standard >>"$list"
changed=(); while IFS= read -r f; do changed+=("$f"); done < <(sort -u "$list" | grep '\.py$' || true)
fail=0 skip=0 kr=0

echo "== (a) lint (${#changed[@]} changed .py vs ${MB:0:8})"
if [ "${#changed[@]}" -gt 0 ]; then
  if command -v ruff >/dev/null; then ruff check --no-cache --select E9,F63,F7,F82 "${changed[@]}" || fail=1
  elif command -v pyflakes >/dev/null; then lint_out="$(pyflakes "${changed[@]}" 2>&1 | grep -E 'undefined name|syntax' || true)"; [ -z "$lint_out" ] || { echo "$lint_out"; fail=1; }
  else "$PY" -m py_compile "${changed[@]}" || fail=1; echo "SKIP undefined-name lint (no ruff/pyflakes; py_compile only)"; skip=$((skip+1)); fi
fi

tests=() area=()
for f in "${changed[@]+"${changed[@]}"}"; do
  case "$f" in
    service/tests/*) case "$(basename "$f")" in test_*.py) tests+=("$f");; esac;;
    service/*) [ "$(basename "$f")" = __init__.py ] && continue
      # tests that import the module by its dotted path (or name it in a string, e.g. monkeypatch);
      # a bare word match pulled in prose and stdlib (`from http.server import`) for server.py
      dot="${f#service/}"; dot="${dot%.py}"; dot="${dot//\//.}"; mod="${dot##*.}"; parent="${dot%.*}"; dq="${dot//./\\.}"
      re="^[[:space:]]*(import|from)[[:space:]]+(service\\.)?$dq([[:space:]]|\\.|,|\$)|[\"'](service\\.)?$dq[.\"']"
      [ "$parent" != "$dot" ] && re="$re|^[[:space:]]*from[[:space:]]+(service\\.)?${parent//./\\.}[[:space:]]+import[[:space:]].*\\b$mod\\b"
      while IFS= read -r t; do area+=("$t"); done < <(grep -rlE --include='test_*.py' "$re" service/tests || true);;
  esac
done
promoted=(); while IFS= read -r l; do case "$l" in ''|'#'*) ;; *) promoted+=("$l");; esac; done <"$PROMOTED"
serial=" $(grep -hv '^#' scripts/premerge-serial.txt 2>/dev/null | tr '\n' ' ' || true)"
known=(); [ -f "$KNOWN" ] && while IFS= read -r l; do case "$l" in ''|'#'*) ;; *) known+=("$l");; esac; done <"$KNOWN"

is_script() {  # run-service-tests.py rule: no pytest import and (__main__ entrypoint or no test_ defs)
  ! grep -qE '^\s*(import|from) pytest' "$1" && { grep -qE '^if __name__ *== *.__main__.' "$1" || ! grep -qE '^(async )?def test_' "$1"; }
}
run() {  # one process per target, like run-service-tests.py isolation
  local t="$1" file="${1%%::*}" res rc last expect="" k id rest s=$SECONDS tmo=0 cmd=(-m pytest -q -p no:cacheprovider "$1")
  [ -f "$file" ] || { echo "MISSING $t"; fail=1; return 0; }
  if [[ "$file" == *_pg.py && -z "${ONLYFANS_TEST_PG_URL:-}" ]]; then echo "SKIP (no ONLYFANS_TEST_PG_URL) $t"; skip=$((skip+1)); return 0; fi
  for k in "${known[@]+"${known[@]}"}"; do read -r id rest <<<"$k"
    if [[ "$id" == "$t::"* ]]; then cmd+=(--deselect "$id"); echo "KNOWN-RED $id (deselected)"; kr=$((kr+1))
    elif [ "$id" = "$t" ]; then expect="$rest"; fi; done
  [ "$t" = "$file" ] && is_script "$file" && cmd=("$file")
  "$TO" -k 5 "$cap" "$PY" "${cmd[@]}" >"$out/$i.o" 2>&1 & echo $! >"$out/$i.pid"
  wait $! && rc=0 || rc=$?; rm -f "$out/$i.pid"; res="$(cat "$out/$i.o")"; last="$(tail -1 <<<"$res")"
  { [ "$rc" = 124 ] || { [ "$rc" = 137 ] && [ $((SECONDS - s)) -ge "$cap" ]; }; } && tmo=1  # the cap fired, not a kill
  if [ "$rc" = 0 ]; then
    # pytest exits 0 on in-test skips/xfails: those cases did not run, so they are not a PASS.
    if [ "${cmd[0]}" = -m ] && tail -1 <<<"$res" | grep -qE '[0-9]+ (skipped|xfailed)'; then
      echo "SKIP (pytest: $(tail -1 <<<"$res")) $t"; skip=$((skip+1)); return 0; fi
    if [ -z "$expect" ]; then echo "PASS $t: $(tail -1 <<<"$res")"; else echo "FAIL $t: known-red but passed; drop it from $KNOWN"; fail=1; fi
  elif [ "$tmo" = 1 ] && [ "$cut" = 1 ]; then echo "DEFERRED to wide: $t (cut at the fast budget)"; def=1
  elif [ "$tmo" = 1 ]; then echo "FAIL $t: per-file timeout ${FTO}s (CHECK_FILE_TIMEOUT_S)"; tail -30 <<<"$res"; fail=1
  elif [ -n "$expect" ] && [ "$(sed -E 's/[0-9]+/#/g' <<<"$last")" = "$expect" ]; then
    echo "KNOWN-RED $t: $expect"; kr=$((kr+1))
  elif [ "$rc" = 5 ] && [ "${cmd[0]}" = -m ] && [[ " ${cmd[*]} " == *" --deselect "* ]] && [[ $last =~ ^[0-9]+\ deselected\ in\ [0-9.]+s$ ]]; then
    echo "KNOWN-RED $t: every test in it is known-red (deselected)"  # pytest rc 5: nothing left to run
  else echo "FAIL $t: $(tail -1 <<<"$res")"; tail -30 <<<"$res"; fail=1; fi
}
# Pool: each target runs in a subshell (own process), output buffered to $out/<i>, counts to $out/<i>.n, printed in order.
targets=() seen=$'\n'  # deduped across sections: first occurrence wins
add() { local t; while IFS= read -r t; do [ -z "$t" ] || [[ $seen == *$'\n'"$t"$'\n'* ]] || { targets+=("$t"); seen+="$t"$'\n'; }; done; }
add < <(printf '%s\n' "${tests[@]+"${tests[@]}"}" "$@" | sort -u); nc=${#targets[@]}
add < <(printf '%s\n' "${promoted[@]+"${promoted[@]}"}"); nd=${#targets[@]}
add < <(printf '%s\n' "${area[@]+"${area[@]}"}" | sort -u)
n=0 total=${#targets[@]} deferred=0 defer_at="${PREMERGE_DEFER_AFTER_S:-}" stop_at="${PREMERGE_STOP_AT_S:-}"
hdr() { [ "$1" = 0 ] && echo "== (b) changed tests ($nc)"; [ "$1" = "$nc" ] && echo "== (c) promoted tests ($((nd - nc)))"
  [ "$1" = "$nd" ] && echo "== (d) touched-area tests ($((total - nd)), CHECK_JOBS=$JOBS)"; return 0; }
flush() { local f s k d; while [ "$n" -lt "$1" ] && [ -f "$out/$n.n" ]; do hdr "$n"; cat "$out/$n"; read -r f s k d <"$out/$n.n"
  fail=$((fail | f)) skip=$((skip + s)) kr=$((kr + k)) deferred=$((deferred + d)) n=$((n + 1)); done; }
for ((i = 0; i < total; i++)); do
  lim=$JOBS; [[ $serial == *" ${targets[$i]%%::*} "* ]] && lim=1  # serial lane: drain the pool, run alone
  while [ "$(jobs -rp | wc -l)" -ge "$lim" ]; do sleep 0.2; flush "$i"; done
  cap=$FTO cut=0; [ "$i" -ge "$nd" ] && [ -n "$stop_at" ] && [ $((stop_at - SECONDS)) -le "$cap" ] && cap=$((stop_at - SECONDS)) cut=1
  if [ "$i" -ge "$nd" ] && { [ "$cap" -le 0 ] || { [ -n "$defer_at" ] && [ "$SECONDS" -ge "$defer_at" ]; }; }; then
    echo "DEFERRED to wide: ${targets[$i]}" >"$out/$i"; echo "0 0 0 1" >"$out/$i.n"; flush "$i"; continue; fi
  ( fail=0 skip=0 kr=0 def=0; run "${targets[$i]}" >"$out/$i" 2>&1; echo "$fail $skip $kr $def" >"$out/$i.t"; mv "$out/$i.t" "$out/$i.n" ) &
  [ "$lim" = 1 ] && { wait || true; }; flush "$i"
done
wait || true; flush "$total"; [ "$n" = "$total" ] && hdr "$total"
[ "$n" = "$total" ] || { echo "FAIL runner: no result for ${targets[$n]}"; fail=1; }
[ "$deferred" = 0 ] || echo "DEFERRED $deferred touched-area tests to scripts/check.sh wide (not started by ${defer_at}s or cut at ${stop_at}s)"

if [ "$fail" != 0 ]; then echo "premerge: FAILED"; exit 1; fi
if [ "$skip" != 0 ] && [ "${ALLOW_SKIP:-}" != 1 ]; then echo "premerge: INCOMPLETE ($skip skipped)"; exit 2; fi
echo "premerge: OK ($skip skipped, $kr known-red, $deferred deferred to wide)"
