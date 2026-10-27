# Requested-media caption correction

Parent backend: `12c7fed4d15d902e999a4c8b9e45823c3f07d9ab`.
Paired dashboard remains `444fd1303858e6d87204efe5811278657571f8f6`.
Branch: `codex/onlyfans-requested-caption-20261027`.

## Cause and scope

The FASTT requested-media path skipped `action_phrase` when the request named
a subject. `ask_clause` also ignored any supplied phrase in that case, so it
rendered only a count, media type and subject. The richer catalogue caption
path and its earlier tests did not exercise this path.

Only named, matching vault-ask captions opt into this correction. Curated-rung,
hook, subject-free and substitute wording keep their existing behaviour. There
are no schema, account configuration, persona, model selection, pricing,
audience, automation, migration or production deployment changes.

## Result

The existing description helper receives the final priced attachment slice,
the same account/fan's six most recent non-unsent messages, and the account's
existing voice block. Stored descriptions are background facts, not text to
paste. Conversation and voice guide phrasing; they are explicitly not evidence
about the attachments. The prompt asks for a natural first-person continuation,
omitting unsupported details and facts not shared across the bundle.

The application still supplies the exact count, media types and subject.
The additional phrase is limited to eight words and 48 characters. The existing
number/price/media-word filter and off-platform guard apply; link-like text,
non-string or oversized output is dropped. Missing or incomplete descriptions,
provider errors and unusable output retain the original basic caption. The
existing wire re-audit, audience check, terms and single-attempt transport remain
in place. Caption generation does not retry delivery.

One additional existing-purpose model call can occur for a named matching ask
with complete descriptions. It uses the current runtime capability, pinned model
and existing budget. No allowance is reset. No extra call occurs for incomplete
descriptions. The other caption paths do not acquire a new call.

## Verification and honest limits

`service/tests/test_fastt_requested_caption.py` runs 21 separate synthetic SQLite
cases with blocked sockets. It covers a complete `NativeEngine.run` turn and the
actual requested-pack planner/native model/native transport boundaries; selected
slice and account/fan scoping; saved voice; mixed-media count/price/order;
fallbacks; incomplete metadata; malformed output; outbound guard; deletion before
send; audience refusal; uncertain transport without a retry; preserved substitute,
unnamed and hook paths; and unchanged saved configuration.

The fixtures inject model responses, selection/pricing decisions where indicated,
and transport receipts. They prove wiring and deterministic controls. They do not
prove that a real model always writes naturally, that stored descriptions match
the underlying media, or that generated claims are semantically correct. Controlled
provider/model verification and review of actual selected media remain release
gates. No live provider/model calls or fan messages were made for this correction.

The existing release workflow runs the new regression with the backup/recovery
boundary, all native suites, paired acceptance and immutable image/rollback work.
The full paired run still requires the previously authorised private dashboard
source handoff or its explicitly provisioned repository-read capability. A missing
capability must remain a failed gate, never a skipped or waived one.

Historical evidence stays with its original pins: 2,182 cases on the original
pair, 2,260 cases on backend `0c4d79a` / dashboard `444fd13`, and the affected backup
proofs on `12c7fed4`. None is relabelled as acceptance for this caption correction.
Record new CI results against the new backend commit in companion evidence.

Production remains undeployed pending complete release evidence, refreshed runtime
fingerprints, the documented backup/writer/migration sequence and explicit release
authorisation. Serial-engine capacity and deferred AUTH-02/03 Flirt work retain
their existing documented limits and are outside this repair.
