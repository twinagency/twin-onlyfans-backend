---
description: Review loop where Codex Sol (medium) is the single writer; Claude reviews
---

Invoke the `review-loop` skill with the Skill tool and follow it exactly, with these overrides. Everything not named here (scope, quiet tree, one writer, read-only Claude reviewers, merge, re-review, commit gate, output) stays as the skill says.

Read root `REVIEW.md` when present and include it in every delegated review/fix prompt. Root wins over `.claude/REVIEW.md`. Select slop checks by relevance; a justified skip is allowed.

Review target: $ARGUMENTS (if empty, use the skill's default target).

**Step 4 Fix: the writer is Codex Sol at medium effort, not Claude.**
- Call `orchestrator_capabilities`, pick the latest `*-sol` model on the `codex` provider instance (never hardcode the ID), and run the fixer with `delegate_task`: `role:"implementation"`, `mode:"async"`, `isolation:"shared"` (same checkout as the loop), `target:{providerInstanceId:"codex", model:<sol>, options:{reasoningEffort:"medium"}}`, and a new `clientRequestId` per fix round. Check the returned provider, model and options match.
- Task prompt is self-contained: the findings file path, the scope (files, base ref), the repo's CLAUDE.md rules that bind writers (reducers, slop harness, no test hooks in `service/`), and the fixer rules from step 4 (ranked order, blockers first, build/typecheck green after each fix, record change + file:line per finding in the findings file, justified won't-fix, no unrelated refactors, no commits, no pushes).
- Wait for the task notification; end the turn instead of polling. No reviewer runs while it edits.
- After it returns, the Claude orchestrator checks `git status`/`git diff --stat` stays inside scope and the build is green before the re-review; out-of-scope edits get reverted or sent back to the next fix round.
- No T3 tools: `nice -n 10 codex exec -s workspace-write -m <sol> -c model_reasoning_effort=medium "<same prompt>"` from the loop's checkout. Codex unavailable: say so and fall back to the skill's Claude fixer.

**Stuck-breaker flips.** A finding still STILL-OPEN or PARTIALLY-CLOSED after 2 Codex fix rounds goes to one Claude fixer (Opus, high effort) with the finding, the failed attempts and the verdicts. Still one writer at a time.

**Codex budget.** Fixer rounds don't count against it. Review-side Codex calls stay as the skill sets them (round 1 review + devil's advocate + final, + repros on risky diffs).

**Output.** Add: fix rounds by Codex vs Claude, and the Sol model ID + effort used.
