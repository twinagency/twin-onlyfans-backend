# OnlyFans sender and mutable-policy register

Prepared 18 October 2026. This is a source inventory for the next offline delivery-authority slice, not approval to enable a sender, reopen the exhausted pilot allowance, or deploy. No provider, model, real account, or remote database was contacted for this inventory.

## Scope and evidence

Backend baseline: `jesse98/twin-onlyfans-backend`, branch `codex/onlyfans-of-b1`, commit `b3a7c0015b3691c34de0bd1c059795f924f518ad`. Dashboard baseline: `jesse98/twin-dashboard`, branch `codex/onlyfans-dashboard-foundation`, commit `a6539d2ca9f2ad3932cc02444a6b99e9140c5c9a`. The current working sources additionally contain the offline foundation changes. Symbols below refer to those working sources; the baseline identifiers do not imply that changes have been committed or pushed.

Coverage was checked against the two captured GitHub recursive trees. All **788 backend code/config candidates** and **635 dashboard code/config candidates** are present, and their original baseline copies match the corresponding Git blob SHA-1 values. The candidate suffixes are `.py`, `.ts`, `.tsx`, `.js`, `.mjs`, `.cjs`, `.sh`, `.yml`, `.yaml`, `.toml`, plus `Dockerfile`. Searches covered provider mutation method definitions, all Python attribute references to them, raw HTTP verbs, automation decorators/dispatch, frontend relay calls, session capture, startup and deployment entrypoints. Candidate counts describe source coverage, not runtime test coverage.

The backend tree has 929 blobs, of which 906 are present. The 23 missing blobs are image/icon assets plus `graphify-out/graph.json`; none is an executable source candidate. The dashboard has 701 blobs, of which 690 are present. Its missing files are ten image assets and `docs/archive/onlyfans-claude-handover-2026-10-18.zip`; the supplied handover was read separately. Generated dependencies, local state, captured sessions, real saved configuration, deployed processes, browser extensions installed elsewhere, remote provider queues, and other machines are outside this inventory.

This register is complete for the named lexical mutation methods and static paths enumerated below. It is **not an exhaustive whole-system/runtime sender assertion**: dynamic calls, actual saved jobs/configuration, externally installed clients and the separate Twin/Telegram native worktree have not been execution-audited. The separate migration/native tree was not edited or imported. Its `twin_bridge`/`native_pilot` code and migrations 0065–0067 are absent from this published backend baseline. The earlier comparative audit describes them, but that description is not evidence that this baseline contains their protections.

Read with the supplied `CONSOLIDATION_PLAN.md`, especially milestones 1 and 3, and the 16 October comparative audit's F2/F3/F8/F9/F11/F17/F18 findings. The source inventory below independently confirms the relevant legacy seams. Historical audit line numbers are not used as current source identities.

## Status meanings

| Status | Meaning |
|---|---|
| Isolated/refused | The dedicated `ONLYFANS_PROCESS_ROLE=api` app does not import or mount the legacy path. This does not disable it in another legacy process. |
| Unconverted | The legacy source can still reach a provider mutation if run with a captured session. A read-only API role is not proof of fleet-wide cutover. |
| Planning/control only | The offline foundation persists inputs, work and control evidence but does not itself authorize a provider POST. |
| External/unverified | The writer or queue is outside this source/process and requires cohort-specific discovery and reconciliation. |

`service/onlyfans_app.py` mounts the scoped read router only. `service/server.py` rejects import whenever the `ONLYFANS_PROCESS_ROLE` key is present, including empty values, directing the API role to `onlyfans_app:app`. `onlyfans_runtime.role_guard.require_legacy_provider_role` is shared by `OFClient.__init__`, session/account factories before file/registry access, `_http_call` before signing/hash lookup, every `_proxy_retry` callback attempt, and multipart upload before each independent session construction and PUT attempt. Thus ordinary construction and reviewed methods on cached/preconstructed clients refuse all present role values, including invalid and empty values. Dedicated roles currently have no approved live provider transport; the text authority remains synthetic. The foundation runtime's default registration is disabled; absent conversation control initializes paused/attention. Legacy deployments without the role key retain their normal behavior and still require cohort cutover/retirement; these guards do not close the whole milestone 3 gate.

The tables below describe legacy behavior before delivery-authority conversion. Their ordinary `OFClient` construction and reviewed call-time egress seams are now refused in dedicated-role processes; deployments without the role key still contain these unconverted paths. Companion delivery changes must update their status with enforcement tests before a release can claim that a particular path is converted.

## Fan-delivery origins and indirect senders

The native-text/operations checkpoint adds a bounded synthetic preparation path
using the existing native composer/turn selector and deterministic guard. It
queues through the shared authority and has no direct send capability. This
does not convert S07's entire legacy run loop, sales paths or deployed cohorts.
See `NATIVE-TEXT-AND-OPERATIONS-2026-10-18.md` for source provenance and limits.

Every row in this table is unconverted legacy source and absent from the dedicated API app. A config default of disabled is not a mandatory transport refusal. Actual saved enablement was not inspected.

| ID | Origin and source symbols | Route/trigger and wire path | Required disposition |
|---|---|---|---|
| S01 | `service/server.py::send_message` | `POST /api/of/v2/chats/{chat_id}/messages` → `OFClient.send_message`; manual text, media, PPV, GIF, reply and tags. Resale checks/attribution/cooldown do not form an idempotent receipt ledger. | Shared authenticated manual intent; bind account/fan/actor and revision, validate before admission. |
| S02 | `server.py::of_restrict_user` | `POST /api/of/v2/users/{user_id}/restrict` defaults `ack=True`, calls S01 with literal `.` before provider restrict, read and hide operations. A restrict action can therefore send a message. | Refuse the implicit acknowledgment or create an explicit approved intent; no hidden fan send inside a stop/restrict command. |
| S03 | `server.py::of_schedule_message` | `POST /api/of/v2/chats/{chat_id}/messages/scheduled` → provider `OFClient.schedule_message` / `/messages/queue`; execution can happen later at the provider. | Remain refused for initial text milestone; inventory/cancel existing remote entries separately. |
| S04 | `server.py::create_scheduled_send`; `automations/scheduled_send.py::run` | `POST /api/of/v2/scheduled-sends` → local `scheduled_jobs` → `OFClient.send_message`. Worker deliberately has no fan lease; human `send_purpose=manual` bypasses include-only audience checks. | Shared durable intent at creation and final admission at fire time; current human provenance must not bypass no-contact, unknown or scope gates. |
| S05 | `server.py::of_send_mass` | Legacy `POST /api/of/v2/chats/messages` → `send_mass_message`. | Refuse until separately converted; do not enable as part of inbound text. |
| S06 | `server.py::of_send_or_schedule_mass` | `POST /api/of/v2/messages/queue` → immediate `send_mass_message` or future `schedule_mass_message`; may enqueue an auto-unsend job afterward. | Refuse both immediate and scheduled branches; future provider delivery is an external writer until reconciled. |
| S07 | `automations/ai_chatter.py::run` | Main native responder sends each text/GIF bubble directly; `sell_lane` dispatches pack delivery. Recurring rules and webhook jobs can both invoke it. | Extract planning later; planner cannot receive a send-capable client. Text becomes persisted actions through one authority. |
| S08 | `ai_chatter.py::_deliver_unlocked`, `_send_free_bubble`, `_maybe_discount_resend`, `_run_nudge`, `_fire_post_buy_rung`, `_run_aftercare` | Independent free-media/free-text, discount resend, proactive nudge, post-buy and aftercare sends; offer-expiry/discount branches also unsend existing messages. | Keep optional branches off/refused until individually converted. A converted main reply is insufficient. |
| S09 | `automations/welcome_chatter_for_info.py::run` | Separate information-gathering conversational engine; sends text parts and GIFs; graduates fans and enqueues handoff work. | Do not start a second responder. Reuse approved planning/context functions only after extraction. |
| S10 | `automations/send_welcome.py::_send_burst`, `_follow_back_one`, `run` | Welcome text/media via `_common.send_dropping_bad_media`, direct GIF send; optional follow-back. | Welcome campaign stays refused; follow mutation needs its own capability classification. |
| S11 | `automations/send_followup.py::run` | Follow-up through `_common.send_dropping_bad_media`. | Refuse until individually converted. |
| S12 | `automations/autoreply.py::run` | Quiet-fan re-engagement direct `send_message`. | Refuse; do not confuse with the single inbound responder. |
| S13 | `automations/reengage_buyers.py::run` | Buyer re-engagement direct DM. | Refuse until individually converted. |
| S14 | `automations/tip_request.py::run` | Proactive tip-request DM. | Refuse; not needed for initial on-request media scope. |
| S15 | `automations/tip_reward.py::_run_image_reply`, `run`, `run_image_reply` | Reactive tip reward or image reply sends text/media. Both `tip_reward` and `image_reply` are separately registered. `server.py::admin_tip_reward_send` can enqueue it manually. | Refuse both kinds. Reactive/exempt classification is not permission to bypass consent/receipt controls. |
| S16 | `automations/make_right.py::_send_bubbles`, `run` | Repair/apology bubbles, with jobs originating in native incident detection and config/rule controls. | Keep preview-only/refused; correction messages are still sends and need separate intents. |
| S17 | `automations/pack_sender.py::deliver`, `_deliver`, `send_pack`, `send_pack_on_ask`; `sell_lane.py::deliver`, `sell`; `pack_farewell.py::send_gather_close` | Native pack/on-ask/farewell sale → direct PPV `send_message`; post-send offer/attribution writes. `plan_*` functions are distinct from wire methods but live in the same modules. | Existing-media capability deferred until text authority passes; freeze item/media/price/ownership revisions and separate planning from delivery. |
| S18 | `automations/reply_mass_funnel.py::_send_step`, `run` | Follow-on funnel text or PPV after a mass message; direct `send_message`. | Refuse; retained historical funnel rows must not reactivate a walker. |
| S19 | `automations/nudge_online.py::run` → `nudge_online_fire.py::run` | Detector creates delayed fire jobs; fire uses `_common.send_dropping_bad_media`. Disabling just the producer does not prove old fire jobs are gone. | Refuse both producer and fire kind; reconcile queued children. |
| S20 | `automations/send_mass_message.py::run` | Shared legacy mass sender, process-local exclusion-list preparation/broadcast lock, then `OFClient.send_mass_message`; writes mass-run/recipient records afterward. | Refuse; a mass-run row is not per-attempt delivery evidence. |
| S21 | `automations/mass_premade.py::run`, `_drip_rest` | Delegates to mass automation and schedules additional items/unsends. | Refuse parent and child jobs. |
| S22 | `automations/mass_nudge.py::run`; `online_blast.py::run` | Each directly calls `OFClient.send_mass_message`; list/online audience resolved partly at provider execution. | Refuse; no per-fan exclusion assumption for list/online broadcasts. |
| S23 | `automations/ppv_send.py::run` | Price segments delegate into mass sender; can create feed post through `post_to_feed` and requeue monthly work. | Refuse all branches; no campaign enablement as side effect of catalogue edits. |
| S24 | `automations/arc_tease.py::run`; `vault_daily_reminder.py::_default_sender`, `run`, `run_consume`; `service/vault_arc.py` | Arc tease/daily reminder delegates into mass sending; approved vault arcs enqueue future jobs. | Keep delivery refused, including pre-existing approved arcs. |
| S25 | `OFClient.upsert_welcome_template`, `delete_welcome_template`, `set_reply_on_subscribe`; corresponding `server.py` routes | Native provider welcome template and `replyOnSubscribe` toggle can cause the provider to send later without the local dispatcher. | Disable/refuse configuration mutation in scoped delivery roles; verify provider state for each cohort. |
| S26 | `OFClient.send_tip`; `server.py::of_send_tip` | `POST /api/of/v2/tips` posts a monetary action. | Refuse; not a free-text capability. |

## Posts, stories, media and auxiliary provider mutations

These are writes even when they do not create a fan DM. All remain outside the dedicated read API and must be refused or separately admitted in the new runtime.

| ID | Source/entrypoints | Mutation and concern |
|---|---|---|
| W01 | `server.py::of_create_post`, `of_edit_post`, `of_delete_post`; `automations/auto_posts.py::run`; `ppv_send.py::post_to_feed`; `ppv_library_config_api.py::post_ppv_to_feed` | Feed creation, edits, deletion, scheduling and optional PPV. A `posted_at` future timestamp moves execution to the provider. |
| W02 | `server.py::of_create_story`, `of_create_story_upload`; `automations/auto_stories.py::run`; `OFClient.post_story`, `post_story_from_url`, `create_story` | Upload/convert followed by story publish; may enqueue cleanup. URL-based helper downloads media before publishing. |
| W03 | `server.py::of_upload_media`, `of_vault_upload_batch`; `vault_upload.py::_upload_one`; `OFClient.upload_media`, `upload_to_vault`, `materialize_to_vault` | Upload signs/PUTs bytes, registers conversion, and materializes vault content by creating a scheduled **carrier feed post** then deleting it. It is not a read-only catalogue import. |
| W04 | `vault_upload.py::_cleanup_carriers`, `_sweep_scheduled`, `sweep`; `server.py::_start_event_pumps` | Carrier cleanup deletes posts; startup starts an immediate/hourly sweep **before** the later test-mode guard. Test mode alone is therefore insufficient provider isolation. |
| W05 | `automations/unsend_messages.py::run`; `ai_chatter.py::_maybe_discount_resend`, `_expire_open_offer`; server per-message and queue DELETE routes | Cancels provider queue entries, unsends DMs, deletes posts/stories and hides media. Cleanup does not send a new DM, but it changes provider state and must preserve/reconcile existing delivery evidence. |
| W06 | `automations/auto_follow.py::gated_follow`, `_run_like_posts`, `_run_like_messages`; server block/restrict/follow-equivalent methods | Follow/unfollow/resubscribe and likes; restrict may hide chat and initiate S02. Do not import this automation as harmless context. |
| W07 | `automations/apply_profiles.py::_push_to_of`; `_common.py::push_nick_and_notes`; server subscription note/custom-name/update routes | Writes fan names, notes and subscription metadata to provider. Fan-fact extraction and provider mutation are separate capabilities. |
| W08 | `audiences.py::_sync_exclude_list_blocking`; `audience_include.py::_find_or_create_fence_blocking`, `_build_autofence_blocking`; `lists.py::_reconcile_of_pair_blocking`, `_sync_do_not_mass`; `automations/audience_sync.py` | Creates and edits provider lists/membership, sometimes inside a nominal audience-preparation/read flow. `account_config_api.py::audience_admit` also adds provider membership. |
| W09 | `vault_ai_api.py` folder publish/apply/create/add/rename/delete/sort/hide handlers; `vault_upload.py::_file_into_folder` | Provider vault-list and media visibility writes; local folder review and provider publication must remain distinct. |
| W10 | Server template, profile, subscription-bundle, promotion, campaign, trial, labels, pins/read/mute/hide and list routes; `promotions_api`, `of_tracking_api`, `trial_links_api` | Direct provider configuration/contact/commerce mutations. Full named method index follows. These routes are not allowed merely because they are not a chat POST. |
| W11 | `scripts/probe_vault_upload.py::probe_shape`, `_upload`, `probe_post`, `probe_queue`, `do_cleanup` | Standalone diagnostic can sign/upload, create/delete carrier post and schedule/cancel a DM. It is an operational sender and must not be run against real sessions during offline tests. |

## Trigger, capture and deployment reachability

| Source | Observed behavior | Conversion obligation |
|---|---|---|
| `service/automation_registry.py::load_automation_plugins`, `register`, `canonical_kind` | Imports every module under `automations`; a new plugin registers without a central allowlist edit. Legacy aliases map to current names. | Dispatcher must deny unregistered capabilities at the provider boundary. A static friendly list of kinds alone is insufficient. |
| `service/automation_executor.py::run_once`, `_materialize_due_rules`, `_drain_due_jobs_once`, `_reconcile_orphans`, `automation_supervisor` | Rules create jobs; one-shot jobs need no enabled rule; run-now can enqueue independently. Process-local locks/pacing are not a cross-process delivery ledger. Failure/cancellation paths can requeue jobs. | Do not let an old running/queued job imply a fresh attempt. Move dispatch state and per-account rate admission to durable authority; preserve uncertain attempts on restart. |
| `service/automation_rules_api.py::switch_kind`, `create_rule`, `update_rule`, `run_now`, `enqueue_one_shot` | Broad administrative configuration/run-now/enqueue controls can trigger registered senders. | Dedicated API must not mount these legacy controls. Future scoped commands validate capability, tenant, actor, idempotency and expected revision. |
| `service/webhook_dispatch.py::on_inbound_message`, `on_inbound_tip`, `on_inbound_image`; `events.py`/`event_transcoder.py` callers | Real-time events choose legacy engine and enqueue work. Per-account JSON opt-in and global `W7_WEBHOOK_DISPATCH_DISABLED` coexist with periodic rules. | Do not run alongside new inbound coordinator for migrated accounts; replay/import cannot trigger old jobs. |
| `service/server.py::_start_event_pumps` | Starts capture/session pumps, transaction polling, automation supervisor, chat seeding, recovery and cleanup; API web replicas would duplicate this if using old entrypoint. | Only explicit isolated API/intake/planner/dispatcher process roles; no accidental scheduler spawn in web replicas. |
| `service/session_bootstrap.py::from_curl`, `_adopt_into_account`; server bootstrap/reload/active-account routes | Captures/imports session material and mutates active account/client ownership. The parser does not simply execute the pasted curl. | Session/account binding must be server-owned and verified; reconnect must not auto-enable work or clear an unknown attempt. |
| `loginExtension` / `noTeleportLoginExtension` interceptors and popups; `desktop/capture/of-capture.js` | Observe existing browser requests and submit captured session to `/admin/session/bootstrap`; desktop request asks `make_active: true`. Interceptors are capture wrappers, not an additional implemented send-authority layer. | Treat session capture as a privileged lifecycle path; never expose credentials in browser data APIs. Official website activity remains an external writer. |
| Backend `app/lib/relay.ts`, `app/next.config.ts`, `web/app.js`, `ui-redesign/infloww-exact/_shared/fastt.js` and chat/broadcast UI | Generic POST/PATCH/PUT/DELETE wrappers and rewrites reach the legacy relay; several retained UI variants share it. | Do not equate a replaced Twin screen with removal of the old API/skin. Transport denial must survive direct HTTP calls. |
| Backend `Dockerfile`, `docker-compose.yml`, `scripts/start.sh`, `scripts/uvicorn-supervisor.sh` | Default launch remains `service.server:app`; compose carries sessions/state and the legacy frontend. | A new role/manifest/image must be explicit and verified. Current offline API isolation does not change the default production entrypoint. |
| Paired Twin dashboard `app/api/onlyfans/**`, `lib/onlyfans/backend.ts`, `lib/onlyfans-preview/server-data-source.ts` | Three production read routes, GET-only server data transport, explicit command refusal; fixture commands mutate synthetic in-memory state only. | Keep unavailable commands disabled until real scoped command API passes integration tests; do not switch fixture commands into real sends. |
| Official app/browser, other machines, old worker services, remote OnlyFans queued messages/posts/welcome rules | Not controlled by the new SQL lease. No remote state was queried here. | Cohort preflight discovers/pauses conflicts and reconciles later external activity. An expired local lease cannot recall a remote POST. |

## Mutable policy and ownership register

This table maps the responsible current components, including policy writers that do not themselves send. It does not adopt every legacy default as approved product behavior. Current code often uses SQLite-specific upserts and has no shared configuration-revision CAS; those administrative paths must not be imported into the PostgreSQL runtime unchanged.

| Policy/state | Current storage and mutable owners | Current readers/enforcement | Required common owner |
|---|---|---|---|
| Tenant, creator/account binding and principal access | `OfCreatorBinding`; legacy `UserAccount`, chatter/employee access, session/account registry and auth/admin transfer routes; Twin dashboard creator/agency identity mapping | Scoped read repository/auth; legacy middleware/context chooses client separately | Server-owned active binding and authenticated command actor, locked/rechecked at dispatch. Mapping changes revoke old authority. |
| Runtime activation and replay cutoff | `db.onlyfans_models.OfRuntime`; `onlyfans_runtime.store.Store.register`, default disabled, explicit activation time | Intake eligibility/work claims; historical imports do not create eligible replies | One account lifecycle owner. Reconnect/import/control change cannot silently activate or reset allowance. |
| Conversation mode, takeover, exclusion and unknown hold | `OfConversationState`, `OfControlAudit`; `Store.control` idempotency/CAS | New work eligibility/claim/complete rechecks; legacy native senders do not consult this common state | Common transactional delivery/control service; every origin including manual obeys no-contact and unresolved evidence. |
| Fan pause/mute/stop/blacklist | `Fan.automation_paused_until`, `Fan.is_muted`, `SkipList`, `Blacklist`; `fans.py::update_fan`; server automation-restrict/restrict routes; `_common.set_automation_restrict`, `set_of_restricted_skip`, `_set_status_and_pause`; native state writers | Native candidate filters, audience helpers and selected per-send checks differ | Defined migration/precedence into common durable control. A legacy unrestrict or pause expiry must not clear explicit no-contact. |
| Include-only and mass exclusion audience | `AccountAiConfig.audience_mode`, `audience_list_id`, `audience_auto_add`; local `List`/`ListMember`; `account_config_api`, `lists.py`, `audience_sync`; provider lists | `audience_include` candidate/fire gates; `audiences` exclusions; manual and named reactive exemptions differ | Explicit capability policy. Do not mistake `off`/`shadow` or exemption metadata for enforcement; initial mass capabilities refused. |
| Automation enablement and scheduling | `AutomationRule` enabled/trigger/steps/quiet/caps; `ScheduledJob` payload/status; `automation_rules_api` plus config APIs' `ensure_kind_rule` and direct enqueue origins | Legacy supervisor/rules/webhook dispatcher; pending one-shots can outlive a disabled rule | One enabled-capability register and durable job/intent lifecycle. Disable parent, queued children and remote schedules independently. |
| AI main chatter, human-yield/payer/offer pacing and scenario knobs | `AccountAiConfig.ai_chatter_config_json`; `scripts_api.py::put_ai_chatter_config`, `set_prompt_shape_all`; settings import | `automations.ai_chatter`, `sell_lane`, context/rhythm/ladder helpers | Versioned planner policy snapshot; final delivery eligibility remains common authority. Bulk prompt-shape writer is a fleet mutation. |
| Welcome, follow-up and information-gathering policy | Rule steps plus `AccountAiConfig.welcome_rules`, `welcome_pinned_json`, `time_activities_json`, `time_images_json`; account config and `automation_preview_api::welcome_pin` | Welcome/follow-up/gather engines; `_common` | Preserve approved creator assets; optional schedulers remain refused. Welcome preview can persist pinned policy, so it is not universally read-only. |
| Structured creator facts, persona, language, voice and model choice | `AccountAiConfig.persona`, `persona_facts_json`, `language`, `voice`, `model`, `model_by_purpose`, `reasoning_effort`; account config/enrich and settings import | `_common` and `_voice`; model precedence is payload override → purpose pin → code/account rules, then model default | One approved creator/config owner and explicit precedence. Do not reuse wrong-channel instructions or let prose overwrite typed facts. |
| Generated daily activities and rhythm/ladder state | `AccountAiConfig.day_log_json`; `RhythmState`, `LadderState`, `LadderQuote`; native generation/state helpers and script configuration | Native context/life/rhythm/closer functions | Planner-owned, auditable context with truthfulness constraints; not a source of independent send permission. |
| Saved fan facts, notes, claims, question/profile state | `Fan`, `FanProfile`, question/trigger/safety records; `fans.py` editor, `gen_info`, `apply_profiles`, native profile/state updates | Native context/planner; some updates push provider notes/nickname through `_common` | Account/fan-scoped context owner, correction provenance and bounded history. Separate local memory writes from provider metadata writes. |
| Nudge and quiet-fan re-engagement | `nudge_config_json`, `autoreply_config_json`, native state/rule payloads; `nudge_config_api` single/bulk, `autoreply_config_api` | nudge detector/fire, mass nudge and autoreply | Optional capabilities remain refused; bulk changes and already-enqueued fire jobs need explicit handling. |
| Event-triggered dispatch | `webhook_config_json`; `webhook_config_api`; env `W7_WEBHOOK_DISPATCH_DISABLED` | `webhook_dispatch` | Migrate account cohort to one intake/coordinator; environment kill flag is a brake, not durable source of authority. |
| Style, typos, typing, restrictions and output filters | `style_config_json`, `banned_words_config_json`; style/banned-word/account config APIs; `_common`, `_banned`, `_voice`, media co-tag configuration | Text pipelines and selected outbound paths; not one universal gate | Planner output validation plus common final policy revision check. Preserve approved behavior without copying competing prompt layers. |
| Tip reward/image reply/tip request | `tip_reward_config_json`; `tip_reward_config_api` and rule steps; native offer/tip states | `tip_reward`, `tip_request`, webhook dispatch | Separate optional capabilities, initially refused. Payment receipt cannot be treated as blanket consent to arbitrary follow-up. |
| Repair/apology policy and resolution state | `make_right_config_json`, `ResolutionLog`; `make_right_config_api`, native incident/correction helpers | `make_right` and main chatter | Preview-only until converted; all outgoing correction bubbles receive ordinary attempts/receipts. |
| Catalogue, media, captions, price, previews, ownership and offers | `CatalogItem`, vault/media tables, `ContentOffer`, `PendingOffer`, purchase/transaction records, PPV config; `scripts_api` catalogue save, `ppv_library_config_api`, vault review/edit/hide, native offer/receipt writers | `pack_sender`, `sell_lane`, `ppv_send`, resale/audience checks | Single canonical commerce owner; freeze payload and price/ownership/catalogue revisions before dispatch and reconcile from accepted receipt/purchase evidence. |
| Vault AI actions/approved arcs | `vault_ai_config_json`, vault review/script/arc records; `scripts_api` patch/approve/cancel and `vault_ai_api` mutations | `vault_arc`, arc tease, reminders/consumer, describe/import helpers | Review permission does not imply sending permission. Refuse publication/sending branches until individually admitted. |
| Funnels and account media mapping | `MassMessageFunnel`, `FunnelAccountMedia`; `funnels_api`; settings import | mass sender and `reply_mass_funnel` | Keep walkers off; preserve historical mapping/evidence without replaying old campaigns. |
| Model spend cap and quota | `AccountAiConfig.daily_cost_cap_cents`, provider/day cost rollup, model pricing/keys; account config, tenant keys, settings import | `llm_client._get_cap_cents`, `_reserve`, `_adjust_daily`; legacy account/provider scope semantics | Atomic durable model budget; preserve existing consumption. This model-spend cap is not the delivery/send cap. |
| Send cap, throttle, conversation freshness and leases | Legacy fan leases/cooldowns, process-local run/write locks and `_next_write_at`; `of_write_paced`; new foundation work generation/revision/token | Native pacing/candidate checks; foundation claims only authorize planning | One transaction reserves durable send allowance/rate slot and attempt, rechecks freshness/control, then admits one provider request. No blind refund/replay after uncertainty. |
| Session/proxy/key/lane/deployment configuration | `account_registry`, captured files, secret store, proxy registry, environment; session/admin/proxy/secrets/tenant-key/lane APIs; `lane_config` file+history | Client factories, signer, LLM providers, admission/concurrency construction | Explicit lifecycle and durable ownership. `lane_config` precedence is env > stored > default and applies on restart; no hot-value illusion. Never put these secrets in browser payloads or ordinary logs. |
| Bulk settings transfer | `settings_transfer_api.py::post_settings_import` writes account config, rules, funnels/media mappings, with backup/sanitization and mode-dependent enablement | Every consumer of imported settings | Treat import as privileged multi-policy command; absent delivery permission cannot be supplied by importing old enabled flags. PostgreSQL-safe transaction/CAS and audit required. |
| Provider-native schedules, auto-welcome, promotions and subscription policy | Provider state changed via OFClient templates/queue/posts/promos/bundles/profile and auxiliary APIs | Provider executes outside local transaction/lease | Cohort manifest inventories remote state. Explicitly cancel/disable where supported and reconcile, retaining evidence if uncertain. |

The existing `audience_include` sender classification is useful discovery evidence. It explicitly names gated/fenced/manual/not-fan-DM/exempt origins, including reactive `tip_reward`, `image_reply` and `make_right`. Its `audit_send_purpose` logs in ordinary operation and raises in tests for missing labels; it is not the new common delivery capability enforcement. Likewise, `FanLease`, process-local broadcast locks and `of_write_paced` spacing are useful legacy brakes but cannot fence an already-admitted remote POST or serialize another process's rate slots.

## Retry and receipt seams requiring conversion

1. `OFClient._http_call` funnels generic signed verbs through `_proxy_retry`, whose catch is `ProxyError`, not a proved-before-transmission signal. The method must not transparently retry an admitted message write on an ambiguous transport outcome.
2. `OFClient._post_dropping_auto_cotag` retries after any 400/422 with the automatically added tag removed. That changes an admitted payload; the new authority must classify a provable rejection and approve a separate action/revision instead of hiding a second POST inside the adapter.
3. `_common.send_dropping_bad_media` retries text-only after an attachment-rejection classification. Text-only fallback is still a new payload and needs recorded disposition. Preserve exact evidence of what landed.
4. `automation_executor.of_write_with_retry` retries a string-matched rate-limit response; `of_write_paced` reserves a process-local slot. Any permitted retry must be based on validated non-acceptance, with a new/linked attempt and no unknown cap refund. Pacing must be durable across dispatchers.
5. Upload/convert bypass generic request through raw `self.http.post`/`put`; multipart `upload_media::_put_part` creates a separate `requests.Session` and calls `sess.put` with its own three-attempt transport retry. Converter registration retries timeout in upload helpers. `materialize_to_vault` calls `create_post` and later `delete_post`. Generic chat-adapter enforcement alone is not full provider-write enforcement.
6. Native success bookkeeping often follows the provider call and can fail independently. A returned response without a validated positive provider message ID is not accepted. Failure after known acceptance repairs history/offer from the receipt without repeating the POST.
7. Legacy jobs can be requeued after cancellation/error and orphan recovery. A dispatching attempt after process death becomes unknown; an expired lease or a new worker must not infer that nothing was transmitted.

## Milestone 3 acceptance obligations and blockers

The initial converted capability should be free text with one shared automatic/manual intent and receipt service. It must carry tenant/account/fan binding, actor, immutable text payload/digest, idempotency key, expected owner/control/input revision, generation/lease ownership, expiry and durable cap/rate reservation. No planner/provider network call may occur while holding the database transaction. The final transaction commits the admission marker before exactly one provider request. A stop/takeover that wins before that marker prevents dispatch; a later stop blocks subsequent actions but cannot recall an already admitted request.

Required positive tests:

- Two processes compete for one intent and for the last account allowance/rate slot; stale owners and stale revisions cannot dispatch.
- Crash before admission creates no request; crash after admission/before response remains unknown with no automatic retry; crash after accepted receipt repairs bookkeeping only.
- Manual, local scheduled, remote scheduled, broadcast, native responder, reaction/repair, restrict-ack, carrier upload/post, templates and raw generic HTTP each route through an admitted capability or raise a refusal. Test both convenience and low-level/raw seams.
- New input, stop, no-contact, takeover, account disable/rebind or session revoke races the final gate. Every pre-admission winner blocks dispatch. Manual cannot bypass no-contact/unknown; resume never silently clears evidence.
- Timeout, malformed/missing ID, response loss, server error or ambiguous proxy error does not become false sent state, released allowance or requeued send.
- Per-bubble receipt and partial result preserve already accepted bubbles. Reconciliation uses provider identity/evidence, not a text-only match.
- Legacy alias/newly registered automation and arbitrary one-shot payload cannot bypass the denied capability set. A harmless classification label is insufficient.
- Actual scoped command HTTP/UI tests enforce operator/tenant/view-as permissions before any intent or control write; test unknown-resolution permission separately.

Release blockers remain until demonstrated: all S/W rows have an enforced routed/refused disposition; shared native source/ownership is accepted without copying the unrelated Telegram migration; populated intent/receipt/cap migrations pass; real session lifecycle and external-writer preflight are defined; operator unknown-resolution audit/evidence exists; default images/entrypoints do not spawn legacy writers; authorized live rehearsal is separately approved. This register alone completes none of those runtime gates.

The paired dashboard still advertises read-only capabilities and refuses real commands. Do not expose a working Send/Resume control based only on this register or a synthetic transport test. Native planner extraction and existing-media commerce remain subsequent work after text delivery authority passes its acceptance gate.

Constructor-boundary regression: `.venv/bin/python -m pytest service/tests/test_onlyfans_role_boundary.py -q --tb=short` passed **5 tests**. Fresh subprocesses test `api`, `worker`, an invalid role and the empty value, each expecting the constructor refusal on `OFClient({})`. The unset-role control expects the existing `KeyError("headers")`. All cases replace the HTTP-session factory with a failing tripwire, proving this tested path creates no live session. This verifies constructor role isolation, not remote transport behavior or legacy deployment retirement.

Call-time isolation regression: `.venv/bin/python -m pytest service/tests/test_onlyfans_egress_isolation.py service/tests/test_onlyfans_role_boundary.py -q --tb=short` passed **56 tests** with fake HTTP only. Cases cover preconstructed clients, POST/PUT/PATCH/DELETE before signing, DM and raw converter POST, role changes between proxy attempts, role changes before/during multipart session creation and retry, a dynamically registered automation using a cached client, factory refusal before session/account access, and unset-role positive controls. Fresh subprocess import hooks reject legacy writers while importing the dedicated API/runtime, and verify early legacy-server refusal for all four role values. The import test also includes `onlyfans_runtime.loopback_transport` whenever that module exists. An AST allowlist enumerates all four current raw HTTP mutation sinks and both session-construction sites, so an additional recognized raw verb/session sink requires review. These tests do not sandbox arbitrary Python code holding raw HTTP handles, retire another deployment, or validate real provider behavior.

## Complete named OFClient mutation index

The following appendix is generated from AST inspection of `service/of_client.py`: seed generic write methods plus raw upload/converter methods, include constant non-GET `request`/`request_json` methods, then close over `self.<method>` calls. It enumerates **80 public mutating methods**. Generic `request`, `request_json` and `_http_call` also accept dynamic verbs and require enforcement even though they are not classified as always-mutating methods. `get`/`get_json` are not write seeds.

External references are lexical Python attribute references excluding tests, the OFClient class itself and the separate Fansly implementation. They identify candidate callers/function boundaries, not runtime call-graph proof. A dash means no such external reference was found; internal OFClient callers still exist. Decorator `.post` and unrelated libraries are excluded from generic wrapper caller rows to avoid false matches. The direct fan origins and indirect delegation above complete the human-reviewed interpretation.

| OFClient method | External non-test Python references |
|---|---|
| `add_label_to_user` | `service/server.py::of_add_label_to_user` |
| `add_media_to_vault_list` | `service/vault_ai_api.py::_push_folder_to_of`; `service/vault_ai_api.py::add_to_of_folder`; `service/vault_ai_api.py::create_of_folder`; `service/vault_upload.py::_file_into_folder` |
| `add_user_to_list` | `service/account_config_api.py::audience_admit`; `service/audience_include.py::_build_autofence_blocking`; `service/audiences.py::_sync_exclude_list_blocking`; `service/automations/audience_sync.py::fast_path_enroll`; `service/automations/audience_sync.py::run`; `service/lists.py::_reconcile_of_pair_blocking`; `service/lists.py::_sync_do_not_mass`; `service/server.py::of_add_user_to_list` |
| `block_user` | `service/server.py::of_block_user` |
| `cancel_scheduled` | `scripts/probe_vault_upload.py::do_cleanup`; `scripts/probe_vault_upload.py::probe_queue`; `service/automations/unsend_messages.py::run`; `service/server.py::of_cancel_scheduled` |
| `comment_on_post` | `service/server.py::of_comment_post` |
| `convert_register` | — |
| `create_bundle` | `service/server.py::of_create_bundle` |
| `create_list` | `service/audience_include.py::_find_or_create_fence_blocking`; `service/audiences.py::_sync_exclude_list_blocking`; `service/lists.py::_reconcile_of_pair_blocking`; `service/server.py::of_create_list` |
| `create_post` | `scripts/probe_vault_upload.py::probe_post`; `service/automations/auto_posts.py::run`; `service/automations/ppv_send.py::post_to_feed`; `service/server.py::of_create_post` |
| `create_promo` | `service/automations/promo_reactivate.py::run`; `service/promotions_api.py::create_promotion`; `service/server.py::of_create_promo` |
| `create_story` | `service/server.py::of_create_story` |
| `create_template` | `service/server.py::of_create_template` |
| `create_tracking_link` | `service/of_tracking_api.py::create_of_tracking_link`; `service/server.py::of_create_tracking_link` |
| `create_trial_link` | `service/server.py::of_create_trial_link`; `service/trial_links_api.py::create_trial_link` |
| `create_vault_list` | `service/vault_ai_api.py::_push_folder_to_of`; `service/vault_ai_api.py::create_of_folder` |
| `delete_bundle` | `service/server.py::of_delete_bundle` |
| `delete_json` | — |
| `delete_list` | `service/server.py::of_delete_list` |
| `delete_post` | `scripts/probe_vault_upload.py::do_cleanup`; `scripts/probe_vault_upload.py::probe_post`; `service/automations/unsend_messages.py::run`; `service/server.py::of_delete_post`; `service/vault_upload.py::_cleanup_carriers`; `service/vault_upload.py::_sweep_scheduled`; `service/vault_upload.py::sweep` |
| `delete_promo` | `service/promotions_api.py::delete_promotion`; `service/server.py::of_delete_promo` |
| `delete_story` | `service/automations/unsend_messages.py::run` |
| `delete_template` | `service/server.py::of_delete_template` |
| `delete_tracking_link` | `service/of_tracking_api.py::delete_of_tracking_link`; `service/server.py::of_delete_tracking_link` |
| `delete_trial_link` | `service/server.py::of_delete_trial_link`; `service/trial_links_api.py::delete_trial_link` |
| `delete_vault_list` | `service/vault_ai_api.py::delete_of_folder` |
| `delete_welcome_template` | `service/server.py::of_delete_welcome_template` |
| `edit_post` | `service/server.py::of_edit_post` |
| `finish_signed_upload` | — |
| `follow_user` | `service/automations/auto_follow.py::gated_follow` |
| `hide_chat` | `service/server.py::of_hide_chat`; `service/server.py::of_restrict_user` |
| `hide_vault_media` | `service/automations/unsend_messages.py::run`; `service/vault_ai_api.py::hide_duplicates`; `service/vault_ai_api.py::hide_item` |
| `like_message` | `service/automations/auto_follow.py::_run_like_messages`; `service/server.py::of_like_message` |
| `like_post` | `service/automations/auto_follow.py::_run_like_posts`; `service/server.py::of_like_post` |
| `mark_all_chats_read` | `service/server.py::of_mark_all_chats_read` |
| `mark_chat_read` | `service/automations/ai_chatter.py::run`; `service/automations/welcome_chatter_for_info.py::run`; `service/server.py::of_mark_chat_read` |
| `mark_chat_unread` | `service/server.py::of_mark_chat_unread` |
| `materialize_to_vault` | — |
| `mute_chat` | `service/server.py::of_mute_chat` |
| `pin_message` | `service/automations/_pins.py::_reconcile`; `service/server.py::of_pin_message` |
| `pin_post` | `service/server.py::of_pin_post` |
| `post` | — |
| `post_json` | — |
| `post_story` | `service/server.py::of_create_story_upload` |
| `post_story_from_url` | `service/automations/auto_stories.py::run`; `service/server.py::of_create_story` |
| `put_json` | — |
| `remove_label_from_user` | `service/server.py::of_remove_label_from_user` |
| `remove_user_from_list` | `service/audience_include.py::_build_autofence_blocking`; `service/audiences.py::_sync_exclude_list_blocking`; `service/lists.py::_sync_do_not_mass`; `service/server.py::of_remove_user_from_list` |
| `rename_list` | `service/server.py::of_rename_list` |
| `rename_vault_list` | `service/vault_ai_api.py::rename_of_folder` |
| `request_signed_upload` | `scripts/probe_vault_upload.py::probe_shape` |
| `restrict_user` | `service/server.py::of_restrict_user` |
| `resubscribe_user` | `service/automations/auto_follow.py::gated_follow` |
| `schedule_mass_message` | `service/server.py::of_send_or_schedule_mass` |
| `schedule_message` | `scripts/probe_vault_upload.py::probe_queue`; `service/server.py::of_schedule_message` |
| `send_mass_message` | `service/automations/mass_nudge.py::run`; `service/automations/online_blast.py::run`; `service/automations/send_mass_message.py::run`; `service/server.py::of_send_mass`; `service/server.py::of_send_or_schedule_mass` |
| `send_message` | `service/automations/_common.py::send_dropping_bad_media._post`; `service/automations/ai_chatter.py::_deliver_unlocked`; `service/automations/ai_chatter.py::_fire_post_buy_rung`; `service/automations/ai_chatter.py::_maybe_discount_resend`; `service/automations/ai_chatter.py::_run_aftercare`; `service/automations/ai_chatter.py::_run_nudge`; `service/automations/ai_chatter.py::_send_free_bubble`; `service/automations/ai_chatter.py::run`; `service/automations/autoreply.py::run`; `service/automations/make_right.py::_send_bubbles`; `service/automations/pack_sender.py::_deliver`; `service/automations/reengage_buyers.py::run`; `service/automations/reply_mass_funnel.py::_send_step`; `service/automations/scheduled_send.py::run`; `service/automations/send_welcome.py::_send_burst`; `service/automations/tip_request.py::run`; `service/automations/tip_reward.py::_run_image_reply`; `service/automations/tip_reward.py::run`; `service/automations/welcome_chatter_for_info.py::run`; `service/server.py::send_message` |
| `send_tip` | `service/server.py::of_send_tip` |
| `set_fan_custom_name` | `service/automations/_common.py::push_nick_and_notes`; `service/automations/apply_profiles.py::_push_to_of`; `service/server.py::of_set_fan_custom_name` |
| `set_fan_note` | `service/automations/_common.py::push_nick_and_notes`; `service/automations/apply_profiles.py::_push_to_of`; `service/server.py::of_set_fan_note` |
| `set_list_pinned_to_chat` | `service/lists.py::_reconcile_of_pair_blocking`; `service/server.py::pin_list_to_chat` |
| `set_reply_on_subscribe` | `service/server.py::of_set_reply_on_subscribe` |
| `set_vault_lists_custom_order` | `service/vault_ai_api.py::sort_of_folders` |
| `sort_vault_lists` | `service/vault_ai_api.py::sort_of_folders` |
| `unblock_user` | `service/server.py::of_unblock_user` |
| `unfollow_user` | `service/automations/auto_follow.py::gated_follow` |
| `unhide_chat` | `service/server.py::of_unhide_chat`; `service/server.py::of_unrestrict_user` |
| `unlike_message` | `service/server.py::of_unlike_message` |
| `unlike_post` | `service/server.py::of_unlike_post` |
| `unmute_chat` | `service/server.py::of_unmute_chat` |
| `unpin_message` | `service/automations/_pins.py::_reconcile`; `service/server.py::of_unpin_message` |
| `unpin_post` | `service/server.py::of_unpin_post` |
| `unrestrict_user` | `service/server.py::of_unrestrict_user` |
| `unsend_message` | `service/automations/ai_chatter.py::_expire_open_offer`; `service/automations/ai_chatter.py::_maybe_discount_resend`; `service/automations/unsend_messages.py::run`; `service/server.py::of_unsend_message` |
| `update_profile` | `service/server.py::of_update_profile` |
| `update_subscription` | `service/server.py::of_update_subscription` |
| `update_template` | `service/server.py::of_update_template` |
| `upload_media` | `scripts/probe_vault_upload.py::_upload`; `service/server.py::of_upload_media` |
| `upload_to_vault` | `service/vault_upload.py::_upload_one` |
| `upsert_welcome_template` | `service/server.py::of_upsert_welcome_template` |
