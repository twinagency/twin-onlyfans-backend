# 07 · Window review (W1, W2, … like infloww W1→W28)

A **window** = every PR merged since the last whole-repo review (`<base sha>..origin/<main>`). You cut it into ~10 **lanes** by area, each lane runs `/rwl` over its files in its own worktree thread, fixes what it finds, ships one PR. An orchestrator thread + a 20-min tick keep the table. Deploy once after the window. Then a closing **thermo pass** counts found/open per lane and a **triage** picks what's worth fixing from the leftovers.

Files per window live in `.scratch/rwl-W<N>/` of the repo (gitignored or committed, your call):
| file | owner | what |
|---|---|---|
| `COMMON.md` | human+AI once | rules every lane reads first (template below) |
| `PROMPTS.md` | AI, human approves | one fenced block per lane, pasted verbatim as the thread's first message |
| `STATE.md` | orchestrator thread only | owner decisions, launch plan, threads table, seeds, tick procedure |
| `HANDOFF.md` | orchestrator | how to resume if the orchestrator dies |
| `<lane>/FINDINGS.md`, `codex-r1.md`, `codex-da.md`, `CORRECTNESS-REVIEW.md` | the lane | its review material |

Order: `cut-window.md` → fill `COMMON.md` → generate `PROMPTS.md` → launch per `STATE.md` → tick → `close-window.md`.
