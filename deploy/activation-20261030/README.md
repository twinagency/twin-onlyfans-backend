# Activation release (2026-10-30)

No schema revision change: alembic head stays `0100_manual_free_media`. The new commands record into the existing
`of_launch_command`, `of_control_audit`, `of_delivery_policy_change` and `of_connection_change` tables
(`of_launch_command.kind` is free text: `bulk_enable`, `future_conversations`, `operating_limits`).

Backend images that change: `api` (new routes) and the processes that import `store.py` / `connections.py`
(`worker`, `fastt-supervisor`/sender). Roll all three together from one commit; the dashboard release is independent
and degrades to "status could not be read" against an older API.

Rollback: redeploy the previous images (`twin-paid-media-repair-20261029:*`). Nothing in the database needs reverting.
Rows written by the new commands are inert audit history for the old code. One behavioural note: connections that an
administrator armed for future conversations (`automate_new_conversations = true`) are ignored by the old code, which
keeps its own lock.

Optional index: `optional-escalation-index.sql` (upgrade and rollback inside).
