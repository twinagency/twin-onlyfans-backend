# Plan C writer-fence inventory

Scope: every path in this repository (`service/`, `app/`, `ui-redesign/`, `fansly/`) that can write to, send to, or
schedule a message for a fan of the pilot creator, and what stops it while the creator's fans belong to the
conversation engine. Branch `claude/plan-c-defects-20261107`. Line numbers are for this branch. Nothing here is a live
check: it is a code reading plus the tests named in the last column.

Terms. "Engine-owned" = a row in `of_engine_owner` for that account/fan (the engine's canonical ownership), and/or the
account's delivery policy `transport_kind = 'fansapi'`. "Pilot account" = an account id listed in the process
environment variable `TWIN_ENGINE_PILOT_ACCOUNTS` (comma-separated, either `of-<id>` or `<id>`). It is read WITHOUT
the ownership store, so it still applies when the store is down. It defaults to empty (no behaviour change).

## A. Writers that go through `of_client.OFClient` (the legacy operator/automation sender)

All of these call `OFClient._owner_fence` (`service/of_client.py:1584`) before any network work, which calls
`moxy_replacement.routing.refuse_engine_owned` (`service/moxy_replacement/routing.py:115`). Refusal is
`engine_canonical_owner_required` for an engine-owned fan; an unreadable store is `engine_owner_lookup_failed`
(held). The only pass for an owned fan is a manual send when every owner row for the fan is mode `human`.

| writer | file:line | fenced | how | test |
|---|---|---|---|---|
| `send_message` (DM) | `of_client.py:1618,1649` | yes | `_owner_fence(chat, purpose)` + `_bridge_boundary` | `test_planc_writer_fence.py`, `test_engine_sender_fence.py` |
| `send_text_once` / `send_text_once_body` (bridge text) | `of_client.py:1691,1702,1710` | yes | `_owner_fence(chat,"other")`; `send_text_once` delegates to the fenced body writer | same |
| `schedule_message` (one fan, provider queue) | `of_client.py:2337,2363` | yes | `_owner_fence(chat, purpose)` | same |
| `schedule_mass_message` | `of_client.py:2369,2424` | yes | `_owner_fence(0,"broadcast")`: refused while ANY fan of the account is engine-owned | same |
| `send_mass_message` (broadcast) | `of_client.py:2433,2500` | yes | same account-level fence | same |
| `send_tip` | `of_client.py:3675,3678` | yes | `_owner_fence(0,"other")` | same |
| `upload_media`, `upload_to_vault` | `of_client.py:2825,3395` | n/a | vault upload, not a fan message | AST guard `test_no_raw_onlyfans_send_outside_approved_layer` |
| operator UI "send message" | `server.py:5162-5245` (`purpose="manual"`) | yes | goes through `OFClient.send_message`; a manual send is only accepted for a fan whose owner row is `human` | fence tests |
| operator UI "schedule message" | `server.py:6902-6960` (`purpose="manual"`) | yes | `OFClient.schedule_message` | fence tests |
| operator UI / funnel mass send | `server.py:5045-5069`, `7335` | yes | `send_mass_message` account-level fence | fence tests |
| Next.js app (`app/hooks/useSendMessage.ts`, `useMassMessages.ts`, ...) and `ui-redesign/` | `app/`, `ui-redesign/` | yes (indirect) | they only call the relay routes above; they hold no provider credential | n/a |

## B. Automation executor and automations

| writer | file:line | fenced | how | gap / action |
|---|---|---|---|---|
| executor pre-run fence (rules sweep and claimed jobs) | `automation_executor.py:558-580`, `:840`, `:1722` | YES after this change | `job_fence_reason` now also refuses every fan-messaging kind for an engine-owned account BEFORE any model call or state write (`engine_canonical_owner_required`) and holds it when the store is unreadable (`engine_owner_lookup_failed`). Before this change only Moxy-selected accounts were fenced here; an engine-owned account's jobs ran their model calls and state writes and died at the final send. | `routing.py:141`; tests `test_executor_*` |
| `scheduled_send` with `send_purpose: manual` | `routing.py:141` (exempt), send-time fence | yes at send time | A human deferral is exempt from the executor fence; the send itself still passes `_owner_fence` (human takeover required) | fence tests |
| automations (`send_welcome`, `mass_nudge`, `online_blast`, `tip_*`, `reengage_buyers`, `make_right`, `ppv_send`, `autoreply`, `arc_tease`, `send_mass_message`, `scheduled_send`, `pack_sender`, `vault_daily_reminder`, `welcome_chatter_for_info`, ...) | `service/automations/*.py` | yes | every send ends in `OFClient` (section A) and the kind is in `MOXY_FENCED_KINDS` | `test_executor_fence_refuses_every_messaging_kind...` |
| non-messaging kinds (`scrape_chats`, `describe_media`, `audience_sync`, profile/story/feed, unsend cleanup) | `routing.py:13-19` | deliberately not fenced | do not write to fan chats | n/a |

## C. TextAuthority (the engine's own delivery authority) and the other transports registered with it

| writer | file:line | fenced | how | test |
|---|---|---|---|---|
| engine reply/offer (`origin=automated` + `engine_decision_id`) | `conversation_engine/delivery.py:16-59` | yes (it IS the writer) | owner row active, epoch, binding, decision `ready`, memory/sequence/connection current | existing engine suites |
| legacy `origin=automated|campaign|engagement` intents for a `fansapi` account | `text_delivery.py:521-527` | YES after this change | `prepare` refuses any non-manual intent that has no engine decision when the account is `fansapi`, for every fan, including a fan with no owner row yet (previously only fans WITH an owner row were refused, so an allowlisted fan before its first plan could be written by a campaign/engagement/native intent) | `test_legacy_mass_scheduled_writers_cannot_prepare_for_a_fansapi_account` |
| manual intent (`origin=manual`) | `text_delivery.py:442` (`route_legacy` returns for manual), `delivery.py:19` | yes | for an engine-owned fan only owner mode `human` passes, plus `staff_owns_takeover` and the conversation policy (human takeover must be effective) | `test_a_human_message_for_an_engine_owned_fan_needs_a_verified_takeover` |
| native transports (`NativeProviderConnector`, `FasttManualTransport`, loopback) for a `fansapi` account | `text_delivery.py:1229-1285` | yes | `dispatch` refuses a transport that does not match `policy.transport_kind` (`transport does not match the account delivery mode`); a fansapi account cannot be switched to/from native (`text_delivery.py` configure) | existing delivery tests |
| FASTT-owned fan with the engine transport | `text_delivery.py:1244-1248` | stall, not double write | if the pilot fan is inside `ONLYFANS_FASTT_SCOPE` / the database selection / `platform_scope_enabled()`, `dispatch` returns `queued` for the engine's transport and FASTT's transport is refused by the mode check. No second sender, but the engine cannot send. | RELEASE CHECK: the pilot account must NOT be in the FASTT scope |
| lab transports (`FansApiSyntheticTransport`, `LocalHttp`, `ProtocolLab`) | `text_delivery.py:1255` | yes | require a `synthetic` policy | existing tests |

## D. Bridge, FASTT runner, native pilot

| writer | file:line | fenced | how |
|---|---|---|---|
| `twin_bridge.coordination.guarded_send` | `twin_bridge/coordination.py:758-775` | yes | `refuse_legacy` first: Moxy owner, then engine owner for EVERY purpose; fails closed |
| FASTT/native pilot runner raw POST | `native_pilot/runner.py:465,574` | yes | the POST is inside `guarded_send(..., purpose="twin_bridge", dispatch_token=...)`; refused for an engine-owned fan |
| native pilot engine | `native_pilot/engine.py:627` | yes | `refuse_legacy(account)` |
| `fastt_manual.FasttManualTransport` (raw `fastt_manual`) | `onlyfans_runtime/fastt_manual.py:87-130` | yes | `send` requires `fastt_owned`; `dispatch` refuses it for a `fansapi` account; the runner's POST is under `guarded_send` |
| FASTT runner model calls / state writes (before the send) | `native_pilot/*` | partly | the SEND is fenced; the runner's own planning is not engine-aware. Keep the pilot account out of the FASTT scope (RELEASE CHECK above) |
| `fansly_backend` / `fansly/fansly_client.py:369` `send_message` | `service/fansly_backend.py`, `fansly/` | not reachable | selected only when account meta `platform == "fansly"`; a `fansapi` policy requires an OnlyFans provider connection, so the pilot (OnlyFans) creator can never resolve to the Fansly client. Not fenced in code; no pilot-reachable path |

## E. Writers the local fence cannot stop (provider side or another process)

| writer | status | what exists / what must be done |
|---|---|---|
| messages ALREADY queued at the provider (scheduled DMs, mass-message queue, `isFromQueue` sends) | not fenceable locally | `fans_api_adapter/events.py:_sent`: a queued send that reaches a fan with an active engine owner row is now recorded on the projection (`queued_send_to_engine_owned_fan`) and flags the fan for attention (`requires_attention`); it is NOT takeover evidence. Before the pilot: an operator must clear/disable the creator's provider-side scheduled and mass queues (release checklist item). |
| the creator or an agent typing in the official app / another device / another tool | not fenceable locally | detected, not prevented: `messages.sent` without our provider id becomes takeover evidence (`events.py` `classify_sent` -> `request_takeover`), after the human grace window |
| another process or box that runs this backend against the same account | outside the process fence | `refuse_engine_owned` reads the shared store, so a second legacy process is refused too (it fails closed when it cannot read it) |

## F. Ownership-store failure policy (what changed)

1. Automated or scheduled writers (any purpose other than `manual`): unchanged and still fail closed
   (`engine_owner_lookup_failed`), at the send (`refuse_engine_owned`) and now also in the executor
   (`job_fence_reason`).
2. A manual (human) send into a DECLARED pilot account (`TWIN_ENGINE_PILOT_ACCOUNTS`) is now held when the store is
   unreadable (`routing.py:115-127`). Before: every manual send passed on a lookup failure, so a person or the UI could
   write into an engine-owned conversation during a store outage.
3. The human takeover path is separate and verified: a manual send passes only if the store positively returns owner
   mode `human` for that fan (`modes == {'human'}`). A label (`purpose="manual"`) alone never passes.
4. Manual sends for accounts outside the pilot scope keep the documented behaviour (not blocked by a store outage).

Operational consequence: if `TWIN_ENGINE_PILOT_ACCOUNTS` is not set in the legacy-sender process, rule 2 does not
apply and item 2's old behaviour remains. The pilot release package must set it in every process that can run
`of_client` (relay server, executor, bridge).
