# AGENTS.md

Rules for every coding agent working in this repo (Claude, GPT/Codex, any other).
Agreed by Ronan and Jesse on 2026-10-29. Same text in every Twin repo.

## You may
- Make a branch named `claude/<task>` or `gpt/<task>` from the latest `main`, in its own worktree.
- Run tests, typechecks and local dev against offline fixtures or staging.
- Push your branch and open a pull request. Report the PR link.

## You may not
- Merge any pull request. Only humans merge, and never their own PR.
- Push to `main` (or `master`).
- Deploy. Production only changes through the repo's GitHub Actions pipeline from a commit on `main`.
  No laptop folders, tarballs, `docker run` on the servers, or "deploy this branch".
- Use the AWS root login, or run SSM commands that change a server.
- Write to the live database (`aouaenqayfltoqrmpitg`). Schema changes go in a migration file in your PR.
  Prices, caps and flags are changed in the dashboard by a human.
- Touch `cnnsllgzskqkvdovyiiv` (old prod) at all.

## When something is broken live
Tell a human. Open a PR with the fix. A human merges it and the pipeline deploys it.
