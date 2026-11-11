---
description: Freshen a repo (correct or delete everything that no longer matches the code)
---

Invoke the `freshen-repo` skill with the Skill tool and follow it exactly.

Scope: $ARGUMENTS (if empty, the whole repository).

Orchestrate the pass yourself and fan out the reading to `model: "sonnet"` subagents, per the
skill. Finish with the dated report and say what it could not verify.
