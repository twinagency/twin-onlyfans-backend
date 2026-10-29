# Manual vault replies release

Feature source: eb29e5c0f2a7d43a96692231f7c89a3b17ae91dd.
Dashboard change: twinagency/twin-dashboard#319, merged as 02694d318164c9a4b2cfcc05102167651580b3b6.

## Behaviour

Operators select exact vault photos/videos, keep them free or set a total USD price, optionally choose free previews, and send with or without a caption. Media is revalidated before delivery; paid ownership, takeover, quarantine, tenant scope and exactly-once receipt rules remain enforced.

## Verified

- 72 PostgreSQL tests pass on the feature commit, covering manual media, delivery and controls.
- The current schema predecessor is 0099_fan_identity_sync. Migration 0100_manual_free_media follows that deployed revision and only widens the manual attachment constraint.
- API, worker and sender images passed an offline canary for free and paid attachment-only wire payloads with networking disabled.
- Live rollout verified the schema, API health and all four replaced processes. No real fan messages were sent for testing.

## Live images

| Process | Image |
| --- | --- |
| API / profile learner | sha256:2d8ff47805c83aeb469c79b76750ae2e42955cb4f1bf6cf41e0cda8b1e821200 |
| Worker | sha256:ec8c71dbecb2da8f788eed5b4d67e465bebbf554bdce744281ea3e8834681fe2 |
| FASTT supervisor | sha256:c36992a21df095a242963e88253084991d75dc4a8eef1aba42af24ebcadf985c |

The overlays retain each running image's profile-learning, fan-identity and other existing code. The worker/sender retain their existing permission context and catalogue locking implementation. Only the reviewed manual-media changes are added to their source variants.

Host-local staging, candidate hashes, stopped originals, compatible rollback images and private configuration snapshots are under /var/lib/twin-release/inbox-media-repair-20261029. The rollout script defaults to read-only preflight. It checks the precise baseline, acquires release locks, refuses active manual delivery, preserves container settings, migrates transactionally, checks health, and records durable compose images. Failure after migration restores compatible images without removing receipts or reversing the database.

This repository's default branch predates the running source. Review is therefore based on the existing recorded current-release base. Do not deploy this checkout wholesale over the current profile and worker overlays.
