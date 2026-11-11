# W<N> rwl — state (orchestrator owns this file)
Prompts: PROMPTS.md. Rules: COMMON.md. Prepared <date>. Window <BASE_SHA>..<HEAD_SHA> (#a–#b).

## Owner decisions (<date>)
- Loop = `/rwl`; reviewers Claude Opus high; ONE fixer. Codex Sol high lens; Astra medium DA/adviser; DeepSeek flash lens (2 calls). Always `-m`.
- Provider for lane threads: <Claude1|round-robin Claude1/2/3>; on weekly limit → next account (see .claude/accounts.md).
- Max in flight <CAP> (start 5; infloww W26 ran 8). No time stop: run until queue empty; limit pauses are fine.
- Tick: scheduled task every 20 min bound to this thread (id below). Deploy: none during window; deploy-all after.

## Launch plan
- Top-level T3 threads (`/rwl` needs its own subagents → lanes can't be subagents). `t3_thread_launch` title `rwl W<N>-<slug>`, workspaceStrategy `{"type":"worktree","baseRef":"<main>","branch":"rwl/W<N>-<slug>","startFromOrigin":true}`, message = that lane's PROMPTS.md block verbatim (+ provider directive).
- Waves A → B as A merges → C last.

## Threads
| # | slug | provider | threadId | status | Ship |
|---|---|---|---|---|---|
| 1 | <slug> | | | queued | |

## Seeds (measured from PR bodies / CI)
- #<pr> <what looked wrong> → lane <k>.

## Tick procedure (every scheduled run; orchestrator never edits code, never merges/deploys)
1. `t3_thread_list` titleContains "rwl W<N>-" → status of every row.
2. Per in-flight thread: `completed` is NOT done — done = last assistant message has a `Ship:` line. Completed + no Ship line → still running (bg agents wake it); `updatedAt` >60 min with no Ship line → stalled: `t3_thread_send` "continue the rwl loop; if blocked, push what is green as a PR and report" (once per 60 min; 3rd stall → failed). Ship line → copy into table; status done / done-unmerged.
3. Waiting (asks something) → answer per COMMON.md: commit/PR/merge pre-approved, merge when green, never deploy, never prod/paid, owner asleep so decide (YAGNI, safest). Merge conflict → "rebase on origin/<main>, re-run checks, merge".
4. LIMIT flag set → once per tick `t3_thread_send` "continue" to ONE paused thread; if it runs, clear flag.
5. Fewer than <CAP> in flight and LIMIT clear → launch next queue items (step above). Add rows.
6. Rewrite this file. All lanes done → disable the tick task, write Summary (+ "Orphan findings" for findings no lane owned), hand to `close-window.md`.

## Summary
- <n>/<L> done, <merged>, <unmerged + why>, <failed>. Deployed: no → deploy-all next.
## Orphan findings (no owning thread)
