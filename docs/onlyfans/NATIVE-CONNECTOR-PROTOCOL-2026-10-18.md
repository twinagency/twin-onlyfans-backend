# Native connector protocol — 18 October 2026

The existing native-client message dialect now runs through the canonical
intake, native worker and common delivery authority against an owned local HTTP
fixture. Database head remains `0079_native_configuration`; no DDL, public route
or frontend source changed. No real provider/session/model adapter is enabled.

## Shared native contract

`service/of_message_contract.py` extracts the existing OFClient paragraph and
message-body formatting into a pure leaf. The legacy client calls this same
formatter; its tag enrichment, logging and existing transport behavior are
preserved. Exact-source parity covers 5,184 combinations, including whitespace,
HTML, Unicode, newlines, prices, media/previews, GIFs, replies and tag inputs.
The canonical connector does not call the legacy client, retry helper or
fallback that removes an automatic tag and posts again.

`onlyfans_runtime.native_protocol` interprets a bounded subset of the native
message object. IDs are positive strict integers; timestamps include timezone;
media IDs/count/order are explicit; money converts to integer cents without
rounding, fractional-cent acceptance or a missing-value fallback to free.
JSON rejects duplicate keys, nonfinite values and oversized input. Supported
paragraph/line-break/entity representation becomes canonical plain text.
Unsupported rich text, tips, queued/retracted/deleted messages and paid-opened
history refuse rather than silently lose their additional meaning. This parser
is not a purchase, refund, read-receipt or financial reconciliation owner.

The sender formats the frozen canonical Dispatch through the shared native
body, including exact files, previews and integer-cent price. If native formatting
would trim or reinterpret canonical text, it refuses instead of rewriting it.
Text/JSON bounds also apply after HTML expansion. All 0–20,000-cent native wire
values are checked against exact cent parsing.

## Scoped local connector and worker intake

`NativeLoopbackConnector` accepts only a `NativeFixtureHandle` minted from an
explicitly owned stdlib HTTP server bound to literal `127.0.0.1`. The handle pins
the canonical account locator, numeric fixture owner and delivery-policy
revision. It accepts no caller URL, session, proxy, cookie, captured signing
rules, credential or environment-based factory. The existing pure `of_signer.sign`
signs requests using fixed synthetic rules; those values are not real secrets.

Before each history operation or send attempt, `/api2/v2/users/me` must prove the
pinned numeric owner. Discovery uses the existing `/chats` query dialect and
explicit offsets. Recovery uses `/chats/{fan}/messages`, descending IDs and the
existing older-than `id` cursor. `hasMore` must be an explicit boolean; short
pages do not imply completion. The whole page validates before Store mutation.
Wrong scope/identities, repeated/unordered IDs, non-advancing cursors, ambiguous
completeness and unsupported records leave durable recovery outstanding.

A `WorkerAccount` may now explicitly supply this same connector as its reader
and transport. Arbitrary readers and separate reader/sender identities are
refused. The existing bounded read step drains up to 100 events and processes
at most one discovery and one conversation-recovery page per account cycle.
Its old `tick()` integer result is preserved; typed progress lets the worker's
`ingress` observation keep counting applied ingress, not HTTP requests/pages.
An explicit `Store.begin_scan` records the initial/reconnect recovery obligation.
There is no live websocket, automatic reconnect scheduler or account activation
factory. Initial registration/arming never implies completed synchronization.

## One attempt and conservative evidence

The common authority checks tenant scope and an exact, current, unused Dispatch
before the identity GET. Forged, foreign and consumed grants cannot trigger that
read. This preflight grants no send rights. After the identity read, the same
`consume_dispatch` rechecks all controls, input/configuration, binding, media,
policy and allowance, then commits the one-use wire marker. Only then does one
POST start. No DB transaction or row lock spans HTTP or model generation.

Both native operations and the existing rehearsal transport reuse one bounded
HTTP implementation. Environment proxy resolution, redirects and retries are
disabled. A fresh client per call prevents cookies from being forwarded between
calls. Only identity-encoded JSON from the exact fixture peer is accepted. Response compression is
refused; raw streaming and declared length are capped at 64 KiB. Each HTTP call
has a configurable absolute deadline of 0.05–5 seconds as well as phase limits.
Identity GET and data GET/POST have separate deadlines. A full read step may
perform two such pairs; shutdown remains cooperative between bounded phases.

A successful native POST must return a scoped outgoing message with the exact
HTML text, canonical text, media IDs/order and cent price. Media also requires
explicit exact preview evidence; locked text must be false. The receipt records
request/response hashes as provenance, never raw session material. The bound
POST URL supplies the conversation context when a recipient field is absent;
an unbound outbound stream message cannot be routed by guessing its recipient.

HTTP success alone is insufficient. Wrong/missing receipt fields, owner mismatch,
missing media preview proof, dropped/slow responses, redirects and 401/403/429/500
responses produce no accepted Message or offer and do not grant a retry/refund.
An uncertain send holds its original unit and known model cost. Accepted media
creates only the existing open offer; it does not establish ownership or revenue.
Accepted projection followed by its first REST/feed observation records intake,
then compares against canonical history without adding a second message/input.

## Verification and remaining work

The one-command demo now discovers a fresh inbound through native-shaped HTTP,
completes durable recovery, retains the real seven-second quiet window, makes
one synthetic native call, posts one shared-format message and shows the
accepted reply/cost/allowance through actual API, Next and Chromium. Existing
Settings, controls, workspace, authentication and mobile acceptance remain.
Run the preceding offline rehearsal command; dependencies and isolation are
unchanged. Detailed current case counts and patch replay are in VALIDATION.md.

This verifies integration with an owned fixture and the repository's existing
native-client dialect. It does not establish today's real platform response
schema, session/signing-rule lifecycle, TLS/proxy compatibility, preview proof
or read completeness. The deliberately strict 64 KiB/subset boundary can refuse
real pages and must be validated before a real adapter is allowed. No fabricated
native response or HTTP error becomes a financial event or delivery decision.

Real account/session/model configuration and delivery/payment reconciliation,
curated vault packs/full semantic policy, catalogue management, authoritative
legacy import, external-writer retirement, production monitoring/supervision,
fleet soak and staged live cutover remain separate implementation/acceptance
work. Existing role guards still reject legacy provider factories and raw
writers in dedicated processes. No Git commit/push, deployment, real account,
paid model, provider request, remote DB mutation, allowance reset or Telegram
runtime change occurred in this checkpoint.
