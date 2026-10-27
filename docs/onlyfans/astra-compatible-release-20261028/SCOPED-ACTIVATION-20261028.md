# Restricted sender activation: 28 October 2026

**Nicky and Naomi resumed at 05:45 Brisbane time. Maile remains excluded. Full public launch is not approved.**

Backend/API/worker/sender/broker source remains 293b23dbdf8dd5e97f27d6c0bef8ef4710415c06, immutable Docker image sha256:b4c3ec13342111c1eadc75ad224ab314b659ff60494d78513b37dbdeb67e33ec. Dashboard remains 4599999761dbf2ff2b517359642d10262324867a, image sha256:fccf205a34fe002c6344062305128f6b302a2e1e0c5d92caf7beac30d2a79c95. Schema remains 0097_release_controls. No application source or prompt changed during this resumption.

[Exact live evidence](evidence/scoped-activation.json) includes the activation, source hashes, scope checks, service health and supervisor status. Both account processes are running. All six containers report zero restarts, tracebacks, schema errors and OOM events during the initial observation. Public HTTPS login is 200. The active API and sender each match all 712 staged Python files.

## Why the scope changed

The earlier deployment check applied account-wide paid-ledger readiness to the entire sender. Actual runtime inspection shows FASTT ordinary replies use native_pilot.service/runner/ledger and twin_bridge authority boundaries; the separate native/catalogue media path uses the commerce freshness guard. This does not justify clearing financial holds or certifying all accounts.

A fresh scope query found zero current-binding unlinked purchases for Nicky (of-176774528, niickybabyfree) and Naomi (of-99421145, imnaominorth), and exactly four already-automated chats on each. Maile (of-323358988, minimai) has four unlinked current-binding purchases, three in already-enabled chats. A budgeted, read-only provider probe confirmed all four payout observations contain no message identity; their description links identify the fan only. The original parser therefore cannot establish an exact payment-to-message link. No link was guessed or written.

Only the sender's deployment allowlist was narrowed to the eight existing Nicky/Naomi chat pairs. Maile and all additional conversations are excluded. The API/reader retain db:* ownership so excluded chats cannot fall back to another sender. No database account/conversation controls, model/persona settings, proxies, balances or counters changed. The 2,000 rolling daily limit remains.

All existing admission, takeover, mute, unresolved-delivery, one-attempt send, immutable receipt and final preflight checks remain. One delivery_unknown attempt remains held. Sent-attempt count was 742 at the initial check, with no new receipt yet. The supervisor reports an all-fans child process per selected account, but each child is restricted by the explicit deployment pair allowlist; that label does not authorize other fans.

## What is still incomplete

- Full public launch, Maile automation, automatic availability for additional chats/accounts, fresh complete paid-ledger coverage, and the controlled paid-provider/model journey are not certified by this restricted resumption.
- A genuine fresh incoming message and provider-confirmed reply are still needed to verify end-to-end delivery after replacement. No manual fan test was sent.
- Exact provider message identity is still required for the four Maile purchases. Existing audited resolution refuses missing identities. Do not infer the link from matching amount/time or clear the commerce hold.
- NativeEngine remains serial; no 250-creator capacity or guaranteed 120-second acknowledgement is claimed.

Do not restore db:* on the sender just to remove the deployment restriction. Resolve the payment evidence/coverage requirements, rerun the prepared full release checks, and record a separate full activation decision. Keep the earlier held-release evidence as historical proof.

Encrypted backups are retained in the approved S3 destination with all four SHA256 readbacks verified. The schema-compatible restricted rollback images remain retained; old writer images must not run against the upgraded schema.
