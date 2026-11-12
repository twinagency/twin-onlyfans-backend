# Fix-orchestrator decision log (backend)
Started 2026-11-12. Team head = origin/claude/commercial-r0-r3-20261111 a253c84c (no newer claude/commercial-* contains it, m).
- D1 step 0: sweep ~50 lane test files (lane-own commits, `git log b --not <team refs>`) against a253c84c; worker Opus high; PG port 55640. Results → .scratch/rwl-programme/FIX-QUEUE.md + docs/fix/FIX-QUEUE.md.
- D2 no known-red list exists in repo (grep m) → "promote" = add lane red test (now green) into service/tests in the fix PR.
