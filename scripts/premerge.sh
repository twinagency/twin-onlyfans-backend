#!/usr/bin/env bash
# FIX-MODE pipeline gate (added in the first fix PR; CI is billing-locked): every fix PR runs it.
#   scripts/premerge.sh [extra test files...]      env: BASE, PY, ONLYFANS_TEST_PG_URL, ALLOW_SKIP=1
# (a) lint python changed since merge-base($BASE) (syntax + undefined names)
# (b) pytest: changed service/tests/**/test_*.py + tests that mention a changed module
# (c) promoted tests listed in scripts/premerge-promoted.txt (one path[::test] per line)
# Exit 0 = OK, 1 = FAILED, 2 = INCOMPLETE (something skipped; ALLOW_SKIP=1 accepts). Pre-existing reds live in
# scripts/premerge-known-red.txt (proven red at base): pytest ids are deselected, a script must end on the listed
# line; any other failure, or a listed script that passes, FAILS. Network blocked via dead proxy.
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
list="$(mktemp)"; trap 'rm -f "$list"' EXIT
git diff --name-only --diff-filter=d "$MB" >"$list"; git ls-files --others --exclude-standard >>"$list"
changed=(); while IFS= read -r f; do changed+=("$f"); done < <(sort -u "$list" | grep '\.py$' || true)
fail=0 skip=0 kr=0

echo "== (a) lint (${#changed[@]} changed .py vs ${MB:0:8})"
if [ "${#changed[@]}" -gt 0 ]; then
  if command -v ruff >/dev/null; then ruff check --no-cache --select E9,F63,F7,F82 "${changed[@]}" || fail=1
  elif command -v pyflakes >/dev/null; then out="$(pyflakes "${changed[@]}" 2>&1 | grep -E 'undefined name|syntax' || true)"; [ -z "$out" ] || { echo "$out"; fail=1; }
  else "$PY" -m py_compile "${changed[@]}" || fail=1; echo "SKIP undefined-name lint (no ruff/pyflakes; py_compile only)"; skip=$((skip+1)); fi
fi

tests=()
for f in "${changed[@]+"${changed[@]}"}"; do
  case "$f" in
    service/tests/*) case "$(basename "$f")" in test_*.py) tests+=("$f");; esac;;
    service/*) [ "$(basename "$f")" = __init__.py ] && continue
      # tests that import the module by its dotted path (or name it in a string, e.g. monkeypatch);
      # a bare word match pulled in prose and stdlib (`from http.server import`) for server.py
      dot="${f#service/}"; dot="${dot%.py}"; dot="${dot//\//.}"; mod="${dot##*.}"; parent="${dot%.*}"; dq="${dot//./\\.}"
      re="^[[:space:]]*(import|from)[[:space:]]+(service\\.)?$dq([[:space:]]|\\.|,|\$)|[\"'](service\\.)?$dq[.\"']"
      [ "$parent" != "$dot" ] && re="$re|^[[:space:]]*from[[:space:]]+(service\\.)?${parent//./\\.}[[:space:]]+import[[:space:]].*\\b$mod\\b"
      while IFS= read -r t; do tests+=("$t"); done < <(grep -rlE --include='test_*.py' "$re" service/tests || true);;
  esac
done
promoted=(); while IFS= read -r l; do case "$l" in ''|'#'*) ;; *) promoted+=("$l");; esac; done <"$PROMOTED"
known=(); [ -f "$KNOWN" ] && while IFS= read -r l; do case "$l" in ''|'#'*) ;; *) known+=("$l");; esac; done <"$KNOWN"

is_script() {  # run-service-tests.py rule: no pytest import and (__main__ entrypoint or no test_ defs)
  ! grep -qE '^\s*(import|from) pytest' "$1" && { grep -qE '^if __name__ *== *.__main__.' "$1" || ! grep -qE '^(async )?def test_' "$1"; }
}
run() {  # one process per target, like run-service-tests.py isolation
  local t="$1" file="${1%%::*}" res expect="" k id rest cmd=(-m pytest -q -p no:cacheprovider "$1")
  [ -f "$file" ] || { echo "MISSING $t"; fail=1; return 0; }
  if [[ "$file" == *_pg.py && -z "${ONLYFANS_TEST_PG_URL:-}" ]]; then echo "SKIP (no ONLYFANS_TEST_PG_URL) $t"; skip=$((skip+1)); return 0; fi
  for k in "${known[@]+"${known[@]}"}"; do read -r id rest <<<"$k"
    if [[ "$id" == "$t::"* ]]; then cmd+=(--deselect "$id"); echo "KNOWN-RED $id (deselected)"; kr=$((kr+1))
    elif [ "$id" = "$t" ]; then expect="$rest"; fi; done
  [ "$t" = "$file" ] && is_script "$file" && cmd=("$file")
  if res="$("$PY" "${cmd[@]}" 2>&1)"; then
    # pytest exits 0 on in-test skips/xfails: those cases did not run, so they are not a PASS.
    if [ "${cmd[0]}" = -m ] && tail -1 <<<"$res" | grep -qE '[0-9]+ (skipped|xfailed)'; then
      echo "SKIP (pytest: $(tail -1 <<<"$res")) $t"; skip=$((skip+1)); return 0; fi
    if [ -z "$expect" ]; then echo "PASS $t: $(tail -1 <<<"$res")"; else echo "FAIL $t: known-red but passed; drop it from $KNOWN"; fail=1; fi
  elif [ -n "$expect" ] && [ "$(tail -1 <<<"$res")" = "$expect" ]; then echo "KNOWN-RED $t: $expect"; kr=$((kr+1))
  else echo "FAIL $t: $(tail -1 <<<"$res")"; tail -30 <<<"$res"; fail=1; fi
}
echo "== (b) touched-area tests"
while IFS= read -r t; do run "$t"; done < <(printf '%s\n' "${tests[@]+"${tests[@]}"}" "$@" | grep . | sort -u)
echo "== (c) promoted tests"
for t in "${promoted[@]+"${promoted[@]}"}"; do run "$t"; done

if [ "$fail" != 0 ]; then echo "premerge: FAILED"; exit 1; fi
if [ "$skip" != 0 ] && [ "${ALLOW_SKIP:-}" != 1 ]; then echo "premerge: INCOMPLETE ($skip skipped)"; exit 2; fi
echo "premerge: OK ($skip skipped, $kr known-red)"
