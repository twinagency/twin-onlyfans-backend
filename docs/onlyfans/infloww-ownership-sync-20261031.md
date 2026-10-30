# Native list ownership sync pilot

The standalone worker mirrors canonical Twin conversation control to three dedicated,
pinned OnlyFans lists: Twin AI assigned, Twin Human control, and Twin Paused or
pending. Names carry a unique suffix so the worker never adopts an agency's
existing list. It never changes AI controls or sends fan messages.

`AI assigned` means automated control, not a promise that a reply is currently
being sent. Other policy, budget, purchase and content holds still apply. Human
control requires an effective takeover with no unresolved attempt. Requested
takeovers and other modes go to the pending list. An AI send in flight retains AI
assignment until a takeover is requested.

The SQLite ledger binds one account and fan scope, records pending work before
provider writes, and acknowledges only after all removals/addition complete and
the canonical state is rechecked. Restarts retry interrupted operations. This is
eventual status visibility, not a lock on Infloww sends. Chatters must still use
Twin's Take over and wait for effective control before messaging through Infloww.

The worker reads a complete account snapshot using keyset pagination with no
20,000-fan cutoff, then performs only changed assignments. Fifty changes per pass
and a one-second gap are defaults, not verified provider capacity figures.
Initial backfills take longer than steady-state deltas. Human edits/deletions of
the dedicated lists are unsupported in this pilot; the ledger must be retained.
Periodic provider-side membership reconciliation is not implemented yet. Do not
describe this pilot as a fully reconciled production integration.

## Pilot commands

Use the existing worker environment and verified encrypted connection loader.
Default operation is shadow/read-only at the provider. Use a durable ledger path.
The process holds an exclusive file lock. Do not share a ledger between scopes.

```
python -m native_list_ownership_sync --account of-323358988 --fan 318022380 --ledger /var/lib/fastt/infloww-sync/maile-test.sqlite3 --once
python -m native_list_ownership_sync --account of-323358988 --fan 318022380 --ledger /var/lib/fastt/infloww-sync/maile-test.sqlite3 --apply --once
```

Repeating the applied command should report zero changes. Daemon operation omits
`--once`. Stop the dedicated process to roll back execution; this preserves the
ledger for a subsequent restart. Lists are advisory and may then become stale.
No automatic cleanup of existing lists or fans is performed on exit.

## Evidence and limits

On 31 October 2026 Brisbane, the deployed verified Maile connection successfully
created test list 1325033637, pinned it, added test fan 318022380, read membership
back, removed the fan and confirmed an empty list. The test list was deleted.
Observed add plus readback took 0.781 seconds; remove plus readback 0.654 seconds.
These timings describe this one test, not Infloww cache latency or 100k capacity.

Offline tests cover uncertain writes and restart, handover ordering, control
changes during synchronization, deleted control rows, account isolation, and a
100,001-fan snapshot/delta. This does not establish full live-account scale.

The remaining UI acceptance check requires the agency's Infloww desktop app:
observe the pilot fan's assignment in two chatter sessions, measure refresh delay,
and confirm the desired inbox filter in Messages Pro or OF Manager. Official
Split Inbox groups are not automatically driven by these native lists. No
Infloww-side send prevention is claimed. Broad activation remains pending that
check and provider reconciliation work.

## Applied pilot evidence

The applied pilot completed one assignment for Maile test fan 318022380. A second
applied run reported zero changes. Sixteen offline tests pass. Managed native list
IDs are AI 1325034694, Human 1325034696, and Pending 1325034701. The live test
identified native membership-removal 404 responses; these are accepted only after
verifying the list itself still exists.

The isolated pilot process uses `/tmp/twin-infloww-pilot-20261031` in the existing
supervisor container, with its durable ledger, PID and bounded operational output
under `/var/lib/fastt/infloww-sync/maile-test.*`. This pilot is not installed as a
restart-managed production service: a supervisor-container replacement stops it.
The repository is the durable source for packaging after the desktop acceptance
check. Before stopping the pilot, verify that the PID file still identifies a
`native_list_ownership_sync` process with this account and test fan. Stop that
process only; do not restart or stop the supervisor container.
