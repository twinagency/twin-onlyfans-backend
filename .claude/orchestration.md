# Orchestration protocol

Opus orchestrates; Opus (effort high) implements. Orchestrator keeps context lean: goal, briefs, decisions, evidence, next steps; delegate bounded work. Model choice is set in the launcher, not here.

Fable = senior adviser, read-only; Opus owns execution. Consult before architecture decisions, scope expansion, costly execution; when evidence conflicts; after two failed attempts; and every 4 completed rounds (a round = a meaningful batch of work, not a tool call or poll). Skip when the task is done. Fable unavailable → say so; never substitute a model or claim a review happened.

Spawn one Fable per task and continue it with `SendMessage` for every consult; re-spawn only if it is gone or the task crosses sessions, seeding the new one from the decision log. Fable records each verdict in the task's decision log; no separate adviser notes file.

Brief Fable evidence-first: goal, constraints, plan, changes since last advice, exact file/diff/run refs, results, open questions. Withhold Opus's preferred answer until Fable's first assessment. No full transcripts or raw logs by default; targeted reads allowed. Fable returns continue / redirect / insufficient evidence + main objection + next concrete check. Opus records its response; "insufficient evidence" means gather it before the decision counts as reviewed.

One short decision log per task in its existing notes: decisions, reasons, risks, what invalidates the plan, Fable advice + Opus response. Each worker gets goal, files, constraints, relevant log entries, acceptance checks. Log survives handoffs; no duplicates.

Adviser agreement ≠ evidence: run the real path. Track tokens (total, Fable), elapsed, rework from available usage reports; mark unknown rather than invent; no telemetry system.

Two levels only: top-level session spawns workers and Fable directly; subagents never spawn subagents (nesting freezes). A subagent needing more agents reports back.
