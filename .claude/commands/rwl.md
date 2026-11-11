---
description: Review loop (parallel review, fix, re-review until clean)
---

Invoke the `review-loop` skill with the Skill tool and follow it exactly.

Read root `REVIEW.md` when present and include it in every delegated review/fix prompt. Root wins over `.claude/REVIEW.md`. Select slop checks by relevance; a justified skip is allowed.

Review target: $ARGUMENTS (if empty, use the skill's default target).
