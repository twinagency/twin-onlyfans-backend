# Rules for every implementer and reviewer in this run (read fully before touching anything)

Repo: <abs path>, branch <branch>. Spec: <path> (numbered items = scope; "Do not build" list is binding). Context only: <paths>.

## Safety (hard)
- Never read `.env`. Never print a key. No DB writes outside tests (test DB `<name>`); read-only SQL allowed: `<psql/sqlite ro cmd>`.
- Never start <long-running prod job>; one is ALIVE now (`pgrep -f <pattern>`): leave it. Do not stop containers, kill node/python, or restart dev servers by hand.
- Never push. Never `git stash`, `git checkout -- <file>`, `git reset --hard`, `filter-repo`, or anything touching another writer's uncommitted work.

## Peer edits that must stay OUT of every commit
Another session has uncommitted edits in: <files>. None of those hunks may be staged. Do not edit <dirs> unless your brief names them.
Staging only your hunks in a shared file: (1) before first edit `git diff -- <f> > /tmp/peer.<name>.patch`; (2) edit; (3) `git add -- <f> && git apply --cached -R -C1 /tmp/peer.<name>.patch`; (4) `git diff --cached -- <f>` shows only your lines; after commit `git diff -- <f> | diff - /tmp/peer.<name>.patch` prints nothing. Fallback: `git show HEAD:<f> > /tmp/base`, apply your edit there, `git update-index --cacheinfo 100644,$(git hash-object -w /tmp/base),<f>`. Never `git add -A`, `git add .`, `git commit -a`; always `git add -- <paths>` then inspect `git diff --cached --stat`.

## Quality (hard)
- YAGNI: only your numbered items; no extra abstractions, flags, "while I'm here" refactors. Follow neighbours' patterns.
- One writer per file at a time; edit only files your brief lists (+ a test file you create). Need another file → smallest change, say so in the report.
- Every change: <typecheck cmd>, <test cmds> green; run touched test files during work, the full set at the end.
- Docs updated where behaviour changes (<doc paths>, CHANGELOG `## [Unreleased]`) or put the exact lines in your report.
- Commits small, one per item; message says why; trailer `Co-Authored-By: <model> <noreply@anthropic.com>`.

## Report (final message)
Commit hashes (one line each) · `file:line` of each change · test counts before/after · skipped + why · doc lines you could not add (verbatim).
