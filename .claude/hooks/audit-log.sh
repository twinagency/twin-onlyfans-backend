#!/usr/bin/env bash
# PostToolUse hook (matcher: *). Appends one JSON line per tool call to .scratch/agent-audit/<date>.jsonl.
# Keeps: session_id, tool_name, first 400 chars of tool_input, cwd, timestamp. Never blocks (always exit 0).
set -u
root="${CLAUDE_PROJECT_DIR:-$PWD}"
mkdir -p "$root/.scratch/agent-audit" 2>/dev/null || exit 0
input="$(cat)"
printf '%s' "$input" | python3 -c '
import json,sys,datetime
d=json.load(sys.stdin)
row={"ts":datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
     "session":d.get("session_id"),"tool":d.get("tool_name"),
     "input":json.dumps(d.get("tool_input",{}),ensure_ascii=False)[:400],"cwd":d.get("cwd")}
open(sys.argv[1],"a").write(json.dumps(row,ensure_ascii=False)+"\n")
' "$root/.scratch/agent-audit/$(date +%Y-%m-%d).jsonl" 2>/dev/null || true
exit 0
