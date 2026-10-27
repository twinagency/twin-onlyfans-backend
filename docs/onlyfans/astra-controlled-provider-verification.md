# Controlled provider verification preparation

Status: prepared scope and commands for review; no outbound authorization and no provider/model call.
Observed by read-only SSM command 2beb1dcb-c690-482e-bf03-694424d1bb8a on 27 October 2026.

- Tenant: twin-internal
- Creator: f80d1add-d10c-4039-90f3-99e6cfc04b91
- Account/owner: of-176774528 / 176774528
- Handle: niickybabyfree
- Proposed controlled recipient: 318022380. It has an existing conversation state,
  automated mode, revision 6. Confirm the recipient is controlled and explicitly
  approve the bounded test before any outbound operation.
- Connection: verified revision 9, observed expiration 2026-10-27T12:04:06.805883.
  Refresh identity/readiness immediately before a future authorized test.
- Existing fixed network: HTTP 198.3.23.34:12323. Credentials remain server-owned.
  Exact fingerprint: 0e91513be4a23a5279d7c39c2dc34a9b6f186eaafec11851b1696c441d0317fc.
- Current saved model AND sealed credential model: openrouter/deepseek-v4.1-flash.
  Credential present. No model or persona settings changed.
- Persona SHA256: dec738d49f12d094c23caa39fdc791f6817bd20bbdba39fd49f9da11f3f02c4e.
- Proposed existing enabled catalogue item: ID 1, video, 300 cents.
  Ordered media: [3271069131,3308786386]; preview: [3271069131].
  Recorded account currency is USD, confirmed by the later read-only supplement.
  Exact provider/catalogue readback must still match US$3.00 before dispatch.
- Manual text: "Twin private verification: please confirm this test message."
- Paid caption: "Private verification of the selected item."
- Synthetic model-draft input: "This is a private integration test. Reply with a
  short greeting." Transport disabled, current saved persona retained.

## Sequence and stopping conditions

1. Verify exact account/owner, recipient ownership/control, connection revision,
   network fingerprint, saved model/key compatibility and catalogue payload.
2. Bounded provider read-only identity/messages/vault/financial-page checks;
   historical pagination uses frozen markers and isolated import writes.
3. One model draft on synthetic input, with transport disabled, after explicit
   authorization for the model request.
4. At most one manual text, one exact approved paid item and one AI reply, only to
   the named recipient. Each uses the existing canonical authority, unique command
   key and single dispatch grant. Record provider receipt and matching readback.
5. Takeover/handback and restart readback. Never reset allowances or replay unknown
   outcomes. Fault injection remains synthetic.

Stop before dispatch if identity, network, revision, model, currency, price or media
differs. Stop after any unexpected recipient, ambiguous send, duplicate attempt,
payload/receipt mismatch or unexplained financial difference. Do not retry an
uncertain send. No purchase is required. No audience or automation expansion.

## Execution gates

Final full-suite evidence, recipient-control confirmation and explicit outbound
approval remain required. Current scoped actor, revisions and the private target
image/schema must be checked in the approved execution window. Preparation is
not evidence of real model availability or provider delivery.

## Verified preparation supplement, 27 October 2026

Corrected source pair: backend 0c4d79aa9f736102aa26bb41e4f7561ab1fff248,
dashboard 444fd1303858e6d87204efe5811278657571f8f6.
The offline candidate predicate supports the saved OpenRouter model, matching
wire model deepseek/deepseek-v4.1-flash and endpoint
https://openrouter.ai/api/v1/chat/completions. No inference was requested.

Read-only SSM bb644d12-6e4e-459f-a3bd-91dc9aa11130 confirmed the sign-in and
managed-proxy containers share twin-onlyfans_default, with the managed-proxy
alias required by the candidate. Command
3cbe7d0a-95e8-4a8d-8b50-6a55fda4db2e found only USD among this account's
520 transaction rows and 276009 offer observations. These row counts describe
stored evidence, not complete financial reconciliation. Proposed paid item
therefore remains US$3.00, subject to exact catalogue/provider readback.

### Exact existing command boundary

The canonical conversation ID is of-176774528:318022380. The command endpoint is
POST /onlyfans/control/v1/accounts/of-176774528/conversations/of-176774528:318022380.
It requires the existing private control credential and
X-OnlyFans-Control-Version: of-controls.v1. No ordinary read credential is usable.
The server-side authenticated management actor must supply the actor field;
never fabricate a session digest. expectedRevision is the freshly read canonical
conversation control revision, not a revision guessed from a previous screenshot.
Revision 6 above is an observation and must not be automatically substituted
after any conflict.

The two existing manual command shapes are:
```json
{"action":"manual_message","expectedRevision":6,"commandKey":"<unique persisted 64-hex key>","actor":"<authenticated management actor>","body":"Twin private verification: please confirm this test message."}
{"action":"manual_message","expectedRevision":6,"commandKey":"<different persisted 64-hex key>","actor":"<authenticated management actor>","body":"Private verification of the selected item.","catalogItemId":1}
```

Prepare and hash these bodies locally for the approved window. Any new revision
or actor requires refreshing the review record before admission. Do not post them
during preparation: on a live connected sender, queueing is an outbound action.
Before approved manual admission, quiesce every sender for this private account
and retain all unresolved ownership/grant fences. After explicit approval, submit
one reviewed body exactly once through the existing management gateway. With
sending still stopped, inspect the frozen canonical intent: tenant, account,
recipient, manual origin, text, ordered media, previews and exact 300-cent USD
paid price must match this packet. A mismatch stops the test before any wire call.
Then permit only the existing canonical authority's one dispatch grant for that
reviewed intent. Do not release unrelated queued work or clear an uncertain hold.
On lost acknowledgement, read its recorded intent and canonical conversation
before any identical-key retry. Never create a new key to get around an ambiguous
send. Stop and retain the existing attempt evidence after any uncertain outcome.

The private API readback path is
GET /onlyfans/api/v1/accounts/of-176774528/conversations/of-176774528:318022380,
using the existing read-purpose credential and the declared read-contract
version. Catalogue readback uses
GET /onlyfans/content/v1/accounts/of-176774528/items/1 with the content-purpose
credential and X-OnlyFans-Content-Version: of-content.v1.
Expected ordered media, previews, price and account are listed above.
Neither command changes saved model, persona, price or media.

The existing runner's read-only preflight command is:
```sh
python -m native_pilot.service --account of-176774528 --fan 318022380 --handle niickybabyfree --twin-context
```
It deliberately omits --enable-send. Do not run a second sending process beside
the deployed supervisor. A future approved live test needs sender quiescence
and one retained owner, rather than a parallel test runner.

### Model-draft and bounded AI command preparation

The prepared helper is scripts/onlyfans_controlled_model_draft.py in the
orchestration checkout. Its default command is read-only preparation:
```sh
PYTHONPATH=service python scripts/onlyfans_controlled_model_draft.py
```
After the specific model-request approval, the exact inference command is:
```sh
PYTHONPATH=service python scripts/onlyfans_controlled_model_draft.py --execute-model-draft --evidence-dir /tmp/twin-controlled-provider-evidence
```
The helper reuses load_verified_connection, NativePilotModel and DailyBudget;
it refuses changed account/tenant/owner, connection revision, model, network or
persona. It limits the draft to one request and 128 output tokens, retains the
existing daily budget, persists an exclusive attempt marker before the call and
holds uncertainty. It constructs no OnlyFans client or send transport. Only the
--help path was executed during preparation; no inference was requested.

For a future authorized one-reply provider check, the existing bounded pilot
supports this exact CLI:
```sh
python -m native_pilot.runner --account of-176774528 --fan 318022380 --handle niickybabyfree --pilot-id astra-controlled-20261027-ai-one --send-limit 1 --max-seconds 120 --twin-context --enable-send
```
Use it only in the reviewed private test environment with one retained sending
owner, after the persistent sender has been quiesced and unresolved grants
remain fenced. Do not run it beside the supervisor, migrate production, override
ownership, manufacture fan input or recreate an uncertain pilot. It uses a fresh
input cutoff and stops after one counted attempt or 120 seconds. It does not use
--content-manifest or --vault-mode, so the separate paid-item check remains the
reviewed manual item command, not autonomous sales. Keep all existing shared
allowance counters and throttle settings. Confirm the target private schema,
connection ownership and artifact digest before execution.

The proposed fresh input from the controlled recipient is:
"Hi, this is the agreed private test. Please say hello."
The recipient must send it themselves after the pilot's readiness event. A
timeout with no new input ends the test; do not fabricate incoming messages.

These are reviewable future commands. No private candidate deployment, schema
migration, sender quiescence, model inference or fan send was performed as part
of this preparation. The execution window, current verified actor/revisions,
controlled-recipient confirmation and explicit outbound approval are release
gates. Any changed connection revision requires a newly reviewed packet.
