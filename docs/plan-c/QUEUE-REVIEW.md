# Queue-event recovery: stopped two-fan text pilot

This change adds a narrowly scoped manual action to the existing authenticated
operator reconciliation owner. It does not classify queue progress as harmless,
automatically approve queue activity, activate the pilot, dismiss other holds,
or modify the immutable original webhook records.

The caller must already be authorized for reconcile in the current pilot.
It is a trusted administrative review, like the existing tip/send-evidence review.
An evidence reference and response hashes record what that operator checked.
They do NOT independently authenticate supplied provider reports. The operator
must first collect authenticated provider evidence on the authorized host,
review both test chats, and retain the source evidence securely. A successful
command cannot stand in for that missing investigation or final independent review.

## Existing endpoint and exact body

Use the existing provider_event item and reconcile endpoint, adding queueReview
to its existing commandKey, actor, evidenceRef, scopeRevision, eventEpoch,
eventKey, payloadSha256 and rawBody fields. queueReview has exactly:

- rawBodies: every distinct retained original body for this one event key,
  1-16 variants, each at most 8192 UTF-8 bytes. Their SHA-256 values must exactly
  match the immutable digest rows. The supplied rawBody must match its own item.
- observedAt: an aware ISO datetime, no more than five minutes old and not future.
- queueListingSha256: hash of the authenticated current provider queue listing.
- pendingQueues: integer zero. An incomplete listing cannot support this assertion.
- externalWritersExcluded: true only after checking the agreed external-writer
  exclusion and actual account-scoped local writer fences.
- fanHistory: one entry per currently approved test fan, with integer fanId,
  retained authenticated response sha256, coveredFrom, coveredThrough and
  activityReviewed=true. Coverage must include the earliest queue creation date
  through observedAt (the explicit coveredThrough cutoff must equal observedAt). Coverage also includes the retained receipt times. Review deletions/missing known messages and delivery
  uncertainty explicitly; lack of a queued flag is not proof of no queue impact.

Only the exact documented chat_queue.updated/finished payload shape is accepted.
Every variant must describe the same queue on the current active provider epoch.
A projected/quarantined webhook requires its existing recovery owner. Missing,
malformed, other-provider, unknown and financial events remain held. The scope
must be stopped, text_only, with both runtime and delivery policy disabled.

## Evidence and concurrency

The review writes a complete group audit and one immutable disposition marker
per exact digest through the existing launch-command ledger. Runtime-row locking
serializes it with intake. New variants invalidate the group's disposition;
unrelated queue groups and independent contact, human, financial, dispatch and
incident holds remain unchanged. Replays grant no authority.

The monitor collector includes current group audit rows from the same account
and runtime binding. The image-owned interval validator independently compares
all raw hashes, provider identity, queue schema, group membership and exact
provider delivery object. A changed payload or newly retained digest is held.

## Release status

This document describes the candidate repair, not an activation verdict.
Qualification, independent review, current provider evidence, refreshed host
composition, credit accounting and the existing launch checks remain required.
