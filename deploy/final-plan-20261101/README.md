# R0: read-only baseline for the final implementation plan (1 Nov 2026)

Stage R0 of the final plan: establish exactly what is installed before anything
changes. These two tools are for the designated operator only. They read; they
never start, stop, recreate, repair, enqueue or toggle anything. The supervisor
is expected to stay intentionally paused.

## 1. Host snapshot

On the production host:

```bash
bash r0_host_snapshot.sh > r0-host-$(date -u +%Y%m%dT%H%M%SZ).txt
```

Records every container (running or stopped), its immutable image id, repo
digests, creation/start time, restart count, revision labels, the newest
twin-repair / twin-onlyfans images, sha256 of the release/compose manifests,
and whether anything references the `conversation-integrity` image (resolves
whether the earlier PR #76 install command ran).

## 2. Database snapshot

Inside an existing backend container that already has `DATABASE_URL`:

```bash
docker cp r0_readonly_snapshot.py <worker-container>:/tmp/r0.py
docker exec <worker-container> python /tmp/r0.py > r0-db-$(date -u +%Y%m%dT%H%M%SZ).json
```

One `SET TRANSACTION READ ONLY` transaction with a 20 s statement timeout,
rolled back at the end. Missing tables/columns are listed under `unavailable`
instead of failing. Per account it reports:

- provider connection status/last error, runtime enabled/revision, delivery
  policy (enabled, media_enabled, delivery_mode, throttle, limits, queue)
- the sales-relevant config keys (contextual mode, pack switches, vault mode,
  caps, discount settings); missing keys print `<missing>`
- vault inventory: live items and items with a non-empty `search_text`
  (non-empty does not prove a useful description)
- newest vault sync / media / ledger / offer-import jobs with status, pages,
  items and last error
- contextual offers by state, conversations by mode/takeover, open text intents

Output contains account ids and config; keep it out of this repository.

## Not covered here (record manually)

Model/provider routing, assembled prompt versions and output budgets per route,
locally overlaid files in each container, and the named deployment operator.
