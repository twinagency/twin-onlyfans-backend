# Twin compatible release — 28 October 2026

Status: prepared, awaiting final acceptance and production cutover. This document is not a deployment claim.

Application source: `293b23dbdf8dd5e97f27d6c0bef8ef4710415c06`. Dashboard: `4599999761dbf2ff2b517359642d10262324867a`. Restricted rollback backend: `a550199c42a9873c59c5bd74f9c2d7bd1793d392`.

The original 2,265-test candidate could not recognize the subsequently deployed `0093_conversation_default_off` branch or preserve the deployed rolling per-fan 2,000-reply quota. The compatible candidate incorporates only those live controls, merges the genuine migration branches at `0097_release_controls`, and extends strict encrypted recovery to include the existing FASTT runtime schema. No production revision is relabelled or stamped backwards. Button-controlled takeover, the requested-caption revert, native AI-05 final-message validation, single-attempt sending and unknown-send holds remain.

The actual forward image passed 26 isolated recovery/control/release checks, the daily-quota SQLite script and the native service script. The actual rollback image passed the populated migration rollback test with all commands closed. A further 17 recovery, takeover and daily-quota checks passed after explicitly selecting legacy allowance mode for the legacy counter fixture. The complete acceptance evidence is collected separately and remains pinned to the application source above.

Release order: preserve fresh fingerprints; stop admission and drain all writers; encrypted CRM/proxy/FASTT, session and configuration backups; authenticate an isolated quarantined restore; ANALYZE the supplied tables; explicitly migrate to 0097; initialize broker recovery and apply the additive session SQL; start reader roles with API commands closed and sender stopped; verify source hashes, account-current financial coverage and real HTTP logout/replay; restore existing capabilities and the persistent sender only. Existing disabled conversations and unresolved attempts remain disabled/held.

Restricted rollback keeps 0097 and additive session SQL. It may start only the proven compatible read-only API/dashboard with command credentials removed. Old sender/worker/sign-in roles must stay stopped. Restoring an old database over newer receipts is prohibited.

No claim is made for 250 creators, guaranteed 120-second acknowledgements, or a completed controlled paid-provider journey. Serial capacity remains a documented limitation. AUTH-02/03 Flirt pairing remains deferred. No manual live fan message is authorized by this packet.

The publication branch also corrects one legacy test fixture to select FASTT_FAN_DAILY_REPLY_LIMIT=0 before checking the lifetime account counter. All existing assertions remain. The corrected fixture and the daily-mode/recovery/takeover cases passed together: 17 passed, zero failures/errors/skips, with the surrounding environment set to daily mode 2000. This is a test-only follow-up; the runtime image stays pinned to 293b23d. The original failed acceptance artifact is preserved; the full pinned shard is rerun with the explicit legacy environment for the release aggregate.
