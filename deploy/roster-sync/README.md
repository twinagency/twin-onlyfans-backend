# Roster sync repair release

Jesse explicitly instructed this agent to deploy this repair on 29 October 2026,
after receiving the tested repair and the missing-main release blocker. This is
a scoped deployment exception for this repair. No IAM, backup deletion, account
flags, model configuration or unrelated deployment changes are included.

The repository still lacks main and a production deployment workflow. This
release preserves the exact observed service images using guarded derivatives:
the worker receives the full repair; the API and existing profile learner receive
the schema and progress changes; the sender receives schema compatibility only.
The dashboard, managed proxy and sign-in service remain on their current images.
The profile learner deployed separately on 29 October is preserved, including
its flags, saved credentials and models. This release does not add model calls.

`rollout.py stage` verifies file hashes and exact base images, builds derivatives
and smoke-tests imports without networking. `cutover` checks the live schema,
takes a host-local full database backup, acquires the existing release locks,
stops the affected processes, migrates, and starts each derivative with its exact
previous environment. It verifies API health, schema, process state and unchanged
services before recording release state. Failed cutover restores the prior
schema and services. Original stopped containers and images are retained.

Inputs in the host-local release directory are `overlay.json` (the changed-file
manifest plus exact UTF-8 candidate contents) and this script. Expected hashes
are pinned. A concurrent release or changed file stops the operation.

Validation before this release: 217 selected regressions passed, including real
PostgreSQL; final 40 focused cases passed after the last changes. Live acceptance
must verify per-creator profile attempts and archive progress, provider failure
classification, and preservation of the existing profile learner. A successful
deployment does not mean the history backlog has finished.
