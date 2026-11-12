#!/usr/bin/env bash
# Pre-merge local proof for fix PRs (CI is billing-locked).
#   scripts/premerge.sh [extra test files...]
# (a) lint changed python vs $BASE (syntax + undefined names)
# (b) pytest: changed service/tests files + tests that mention a changed module
# (c) promoted tests listed in scripts/premerge-promoted.txt
# *_pg tests run only when ONLYFANS_TEST_PG_URL is set. Network is blocked via a dead proxy.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT"
BASE="${BASE:-origin/claude/commercial-r0-r3-20261111}"
PY="${PY:-$( [ -x .venv/bin/python ] && echo .venv/bin/python || echo venv/bin/python )}"
export PYTHONPATH="$ROOT:$ROOT/service:$ROOT/service/tests:$ROOT/ops/plan-c-supervision${PYTHONPATH:+:$PYTHONPATH}"
export HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 NO_PROXY=
changed=$( { git diff --name-only --diff-filter=d "$BASE"; git ls-files --others --exclude-standard; } | sort -u | grep '\.py$' || true)
fail=0

echo "== (a) lint ($(echo "$changed" | grep -c . || true) changed .py)"
if [ -n "$changed" ]; then
  if command -v ruff >/dev/null; then ruff check --no-cache --select E9,F63,F7,F82 $changed || fail=1
  elif command -v pyflakes >/dev/null; then out=$(pyflakes $changed 2>&1 | grep -E 'undefined name|invalid syntax|SyntaxError' || true); [ -z "$out" ] || { echo "$out"; fail=1; }
  else "$PY" -m py_compile $changed || fail=1; echo "WARN: no ruff/pyflakes, py_compile only"; fi
fi

tests=()
for f in $changed; do case "$f" in service/tests/test_*.py) tests+=("$f");; esac; done
for f in $changed; do
  case "$f" in service/tests/*) continue;; service/*) ;; *) continue;; esac
  mod=$(basename "$f" .py); [ "$mod" = "__init__" ] && continue
  while IFS= read -r t; do tests+=("$t"); done < <(grep -lw "$mod" service/tests/test_*.py || true)
done
promoted=()
[ -f scripts/premerge-promoted.txt ] && while IFS= read -r l; do
  case "$l" in ''|'#'*) ;; *) promoted+=("$l");; esac; done < scripts/premerge-promoted.txt

run() {  # one pytest process per target, like run-service-tests.py isolation
  local t="$1" file="${1%%::*}"
  [ -f "$file" ] || { echo "MISSING $t"; fail=1; return; }
  if [[ "$file" == *_pg.py && -z "${ONLYFANS_TEST_PG_URL:-}" ]]; then echo "SKIP (no ONLYFANS_TEST_PG_URL) $t"; return; fi
  if res=$("$PY" -m pytest -q -p no:cacheprovider "$t" 2>&1); then echo "PASS $t: $(tail -1 <<<"$res")"
  else echo "FAIL $t: $(tail -1 <<<"$res")"; tail -30 <<<"$res"; fail=1; fi
}
echo "== (b) touched-area tests"
for t in $(printf '%s\n' "${tests[@]+"${tests[@]}"}" "$@" | grep . | sort -u); do run "$t"; done
echo "== (c) promoted tests"
for t in "${promoted[@]+"${promoted[@]}"}"; do run "$t"; done

[ "$fail" = 0 ] && echo "premerge: OK" || { echo "premerge: FAILED"; exit 1; }
