#!/usr/bin/env bash
# PreToolUse hook (matcher: Bash). Blocks any `git push` while the tree is on main/master
# or when the refspec names main/master. Exit 2 = block; stderr is shown to the agent.
# Branch-aware: pattern denies in settings.json match text only; this checks git state.
set -u
input="$(cat)"
cmd="$(printf '%s' "$input" | python3 -c 'import json,sys
try:
  d=json.load(sys.stdin); print(d.get("tool_input",{}).get("command",""))
except Exception: print("")')"
cwd="$(printf '%s' "$input" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("cwd",""))
except Exception: print("")')"
case "$cmd" in *git*push*) ;; *) exit 0 ;; esac
# refspec naming main/master
if printf '%s' "$cmd" | grep -Eq '(^|[[:space:]:])(main|master)([[:space:]]|$)|:(main|master)\b|refs/heads/(main|master)'; then
  echo "BLOCKED by guard-main-push: refspec names main/master. Push a branch, open a PR; a human merges main (deploys prod)." >&2; exit 2
fi
# force push: only on own task branches (jaka/*, kit/*, fix/*, feat/*); never claude/*, codex/*, main
if printf '%s' "$cmd" | grep -Eq -- '--force|(^|[[:space:]])-f([[:space:]]|$)|[[:space:]]\+[A-Za-z]'; then
  tgt="$(printf '%s' "$cmd" | grep -Eo '(jaka|kit|fix|feat|claude|codex)/[^[:space:]:]+' | head -1)"
  [ -z "$tgt" ] && tgt="$(git -C "${cwd:-.}" symbolic-ref --short -q HEAD 2>/dev/null || true)"
  case "$tgt" in jaka/*|kit/*|fix/*|feat/*) ;; *) echo "BLOCKED by guard-main-push: force push allowed only on own branches (jaka/*, kit/*, fix/*, feat/*), target='$tgt'. Never rewrite claude/*, codex/*, main." >&2; exit 2 ;; esac
fi
# repo dir: honour `git -C <dir>`
dir="$cwd"
if printf '%s' "$cmd" | grep -Eq 'git[[:space:]]+-C[[:space:]]+'; then
  dir="$(printf '%s' "$cmd" | sed -E 's/.*git[[:space:]]+-C[[:space:]]+([^[:space:]]+).*/\1/')"
  case "$dir" in /*) ;; *) dir="$cwd/$dir" ;; esac
fi
br="$(git -C "${dir:-.}" symbolic-ref --short -q HEAD 2>/dev/null || true)"
case "$br" in
  main|master) echo "BLOCKED by guard-main-push: working tree is on '$br'. Pushing main deploys prod. Create a branch first." >&2; exit 2 ;;
esac
exit 0
