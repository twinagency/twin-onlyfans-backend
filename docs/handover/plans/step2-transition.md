# Step 2: retained authority -> commercial account contract (design, read-only)

Source read: backend `claude/commercial-r0-r3-20261111` at be7108d (head 0133), dashboard sibling tree. Nothing executed. Paths relative to `backend/service/` unless noted. "Verified" = read in code; "proposed" = design.

## 1. Verified current behaviour

**Scope table.** `OfEnginePilotScope` (db/onlyfans_engine_models.py:474-501): PK `scope_id`, `UNIQUE(account_id)` (:485), CHECK `scope_id>=1` (:481, was `=1` until 0132), status in configured/active/stopped, `(status='active')=(active_since IS NOT NULL)`, counters `provider_requests_used`, `provider_credits_reserved`, `config_json` TEXT + `fingerprint`, `last_attempt_json` (0133). Preparation rows use ids from `of_engine_preparation_scope_seq` (starts 2; preparation.py:202).

**Guard trigger** (db/migrations/versions/20261111_0800_account_preparation.py:7, installed :17-20; hash-pinned in db/schema.py:451-460, rank>=132): (a) DELETE refused; (b) for `scope_id>1`: contract must be `preparation.v1`, and `status='active'` or `active_since` set is refused (this is the "cannot authorize delivery" clause); (c) UPDATE of `scope_id>1` may not change provider_account / creator_id / qualification.mode; (d) INSERT must be rev 1, configured, counters 0; (e) UPDATE may not change scope_id/account_id/binding_id/created_at, counters may not decrease; (f) status/active_since/config/fingerprint change needs revision+1, active rows cannot change config; revision may not change alone. Nothing blocks scope_id>1 reaching `stopped`.

**pilot_scope.py (legacy, singleton).** `validate` :50-143 requires exactly 2 fans (:86-87), one photo at 300 cents (:99-100), key set fixed (:51-52). `current` :281-294 queries `scope_id==1` (:282) and returns `(None,None)` when the row belongs to another account (:284-285), so for every other account (including preparation accounts) "no scope" silently means "not a pilot". `require` :297-301, `authorize` :304, `authorize_fan` :309, `configure` :528-595 (looks up `scope_id==1` :539, inserts `scope_id=1` :586, refuses another account :540), `set_status` :670-725 (text-only activation :598-667; stop of text-only goes through `stop_runtime_in_session` :703-715; full mode stop only flips the row), `describe` :728-738, `reserve_provider_request` :741, `ProviderRequestGuard` :758, stopped-backfill guard :777-889, trial takeover exception :892-951 (keyed by binding + `current`).

**Preparation lookup.** conversation_engine/preparation.py `current` :132-141 selects by `account_id`, returns `(None,None)` for scope_id==1, validates with an exact-key `validate` :114-130 (fixed 5 keys, `contract_version==preparation.v1`). `configure` :166-219 refuses an account that has row 1 (:192) and never changes contract. `assert_send_allowed` :143-147 raises for ANY scope_id>1 row, whatever status or damage.

**Delivery admission refusals for preparation rows.** `Store._delivery_runtime` (onlyfans_runtime/store.py:192-209, calls preparation.assert_send_allowed :207; used at store.py:580,642, text_delivery.py:564,754,935,1441, campaigns.py:344,426, fastt_manual.py:124,202) and `TextAuthority._configure_in_session` (text_delivery.py:273-275, only when `enabled` or `media_enabled=True`). Preparation also requires runtime and policy disabled (`_disabled` :149-153) and its guard needs `status=='configured'` (:273-300).

**Who looks up the scope.** Supervised worker is one process per account: `build_supervised_pilot` reads env TWIN_PLAN_C_TENANT/ACCOUNT (model_transport/pilot_supervised.py:519-565), `require(s,runtime)` :532; `SupervisedPilot.validate` :160-180 re-requires an ACTIVE scope each tick; `plan_c_worker.main` :185-205 serves one pilot on one port. Model call stamps `(revision,fingerprint)` via `ModelLedger._pilot_guard` (conversation_engine/model_ledger.py:118-135). Ops tools are hard bound to the legacy account: ops/plan-c-supervision/supervisor.py:29 (`scope_id=1 and account_id='of-323358988'`), onlyfans_runtime/monitor_incident_recovery.py:77-81,107.

**Config/BFF owners.** Backend: onlyfans_commands/configuration_api.py:153-189 (GET/POST `/accounts/{id}/preparation`, exact actions configure|stop, 16 KiB, agency actor via `agency_scope.actor_allowed` :59); onlyfans_commands/engine_approval_api.py:108-140 (`pilot-scope` GET/configure/status, actor `op-session:<64hex>` named in `config.operator`). Dashboard: `app/api/onlyfans-configuration/accounts/[accountId]/preparation/route.ts` (agency session, `ctx.canManage`), `lib/onlyfans/preparation-backend.ts` (CONFIGURATION token), `lib/onlyfans/preparation.ts`, `components/onlyfans-preview/Preparation.tsx`; Plan C side `lib/onlyfans/plan-c-backend.ts:64-69` (CONTROL token; only reads `authorization_mode`), `plan-c-route.ts:22`, `plan-c-owner-core.ts:17`.

## 2. Minimal design

**Principle.** One physical row per account (`UNIQUE(account_id)` already exists). The preparation row is UPDATED in place (same scope_id, account_id, binding_id, created_at, counters, last_attempt_json, audit history) from `preparation.v1` to `commercial.v1`. Legacy row 1 keeps no `contract_version` and stays frozen as a legacy contract; no path converts it (decision D1). Old code (0133 image) reading a commercial row fails closed twice: `preparation.current` rejects the key set, and `assert_send_allowed` still refuses any scope_id>1 row. So rollback of code cannot deliver.

**Contract `commercial.v1`** (superset of preparation.v1, so the 0132 guard identity paths keep working):
```
{contract_version:"commercial.v1",
 provider_account, creator_id, qualification{mode,reference},          # immutable, copied from preparation
 limits{provider_requests, provider_credit_budget, provider_credits_per_request,   # same names/same counters
        send_units, daily_send_units, throttle_seconds},
 fans:[ids, 1..100],                  # explicit approved audience (key name kept so ~40 existing fan checks work)
 creator_profile, authorization_mode, delivery_mode:"text_only"|"full",
 operator{actor,name,permissions}, model{...as pilot...},
 catalogue{account_id,binding_id,complete,evidence_ref,valid_until,snapshot_hash,snapshot_kind:"commercial-catalogue.v1"} | absent only if text_only,
 transition{from:"preparation.v1", from_revision, from_fingerprint, command_key}}   # immutable lineage
```
No `product`, no 2-fan or $3 rule. Product approval lives in the `catalogue` snapshot (hash computed by Step 3's owner; Step 2 only validates shape/expiry/binding like `current_offer_coverage` pilot_scope.py:146-168). Runtime authority is `operator`+`authorization_mode`+`status`. They are separate fields written by separate commands.

**Dispatch.** New `conversation_engine/scope_contract.py`: `load(s, runtime, lock=False)` selects by `account_id`, reads raw `contract_version`, returns `(row, config, kind)`, kind in `legacy|preparation|commercial`; each kind has its own `validate`, fingerprint check as today. `pilot_scope.current/require` return the row only for `legacy` and `commercial` (preparation still `(None,None)`, unchanged behaviour); `preparation.current` returns preparation AND commercial-in-configured (projection to the 3-key limits view) so vault refresh keeps drawing on the same counters before launch. All consumers go through these two functions, so the singleton rewrite is one place.

**Migration `0134_commercial_scope_contract`** (file `db/migrations/versions/20261111_1300_commercial_scope_contract.py`, down_revision `0133_preparation_retry_state`; add constant, `HEAD`, `REVISION_RANK`, pinned guard hash `>=134` in db/schema.py:80-92,451-460; model columns tagged `onlyfans_since`):
- `ADD transition_command_key VARCHAR(64) NULL`, `commercial_since TIMESTAMP NULL`; composite FK `(account_id, transition_command_key)` -> `of_launch_command(account_id, command_key)` RESTRICT (audit row must exist first). No data rewrite, no new budget row.
- Replace guard function (CREATE OR REPLACE, keep trigger). Delta vs 0132, nothing else removed:
  1. Contract rule becomes: scope_id>1 contract in (`preparation.v1`,`commercial.v1`); OLD=commercial -> NEW must stay commercial (no downgrade); scope_id=1 contract must not change (no key appears).
  2. `active`/`active_since` forbidden for `preparation.v1` exactly as today; allowed for `commercial.v1` only if `transition_command_key IS NOT NULL`, `commercial_since IS NOT NULL`, OLD.status in (configured,stopped), `operator.actor` present.
  3. preparation -> commercial UPDATE allowed only when: NEW.revision=OLD.revision+1, NEW.status='configured', NEW.active_since NULL, `transition_command_key`/`commercial_since` newly set, `config.transition.from_fingerprint = OLD.fingerprint` and `from_revision = OLD.revision`, provider_account/creator_id/qualification.mode unchanged, `limits.provider_requests >= used` and `provider_credit_budget >= reserved`.
  4. `transition_command_key`, `commercial_since`, `config.transition` immutable once set; commercial `limits.provider_*` may never drop below current counters.
  5. INSERT of scope_id>1 still must be `preparation.v1` configured unused (a commercial row cannot be created from nothing).
- `downgrade()`: refuse if any commercial row exists (same pattern as 0132/0133), else restore 0132 guard and drop columns/FK.

**State machine (commercial rows).** `configured -> active` (launch decision) ; `active -> stopped` ; `stopped|configured -> configured` via configure (revision+1, `active_since` NULL, config immutable while active) ; `stopped -> active` re-launch ; never back to preparation ; never DELETE. preparation: `configured|stopped -> commercial/configured` only by `promote`.

**Who may request what (all idempotent via `of_launch_command`, `expected_revision`, 64-hex command key, as pilot_scope `_prior/_audit`).**
| command | actor | effect | refuses unless |
|---|---|---|---|
| `promote` | agency managing actor (`actor_allowed`, configuration purpose) | preparation->commercial/configured; writes lineage; grants nothing | runtime+policy disabled, no dispatching/unknown intents, link `connected` for config.creator, limits >= counters; import completeness NOT required (activation checks it) |
| `configure` | actor named in current `config.operator` with `configure` (existing rule `authorize` :304) | edit audience/profile/catalogue/model/limits in configured/stopped | audience removal blocked while unresolved intents (reuse pilot_scope.py:556-568), limits >= counters, catalogue snapshot unexpired |
| `activate` (launch decision) | named operator with `activate` | status active, `active_since` | approved profile (`require_profile`), non-empty audience, catalogue present+current unless text_only, limits, import `complete` job, connection/link verified, history recovered per fan (reuse `_activate_text_only` checks :608-667), provider budget headroom; full mode additionally calls the Step 3 hook `catalogue_ready()` which returns Refused until Step 3 lands |
| `stop` | agency actor OR operator `stop` | status stopped, runtime disabled, queued intents cancelled via `stop_runtime_in_session` (both delivery modes, unlike legacy full mode) | none beyond revision |
Imports and preparation passes never call `promote`/`activate`; no new code path writes `status='active'` except `set_status`. Product approval (catalogue snapshot) and runtime authority are two fields; same human doing both is allowed but both audited (D3).

**Per-account lookup.** Replace `scope_id == 1` with `account_id == runtime.account_id` in `pilot_scope.current/configure`; `require/describe/set_status/authorize_fan` follow. `trial_takeover_exception` and `set_trial_takeover_exception` additionally require `row.scope_id == 1` (legacy exception never covers commercial). Worker: unchanged one-process-per-account model; ten creators = ten env sets (TWIN_PLAN_C_ACCOUNT, port, webhook secret file, model env with `enabled_accounts=={account}`, profile). `build_supervised_pilot` replaces `config['product']` checks with `scope_contract.profile_items(config)` (legacy: product id; commercial: no item pin until Step 3, text-only requires `approved_item_ids` empty).

**Stop / takeover / emergency stop per account.** Per fan: `Store.control` (store.py:434-449) already authorizes fans in `config['fans']`; unchanged, works for commercial rows. Scope stop: command above. Emergency stop: `launch_operations.control_account` (:115-150) keeps disabling the runtime; new in-session helper `stop_scope_in_session(s, runtime, ...)` is called from it when a commercial row is active, setting `status='stopped'` revision+1 in the same transaction, so runtime and scope cannot disagree. `resume` on a commercial account is refused unless scope status is active (resume must not bypass the launch decision). Worker `validate()` already stops planning/sending when scope not active; webhook intake keeps recording (pilot_supervised.py:186-200).

**API/BFF.** Backend: `GET|POST /onlyfans/configuration/v1/accounts/{id}/commercial` in configuration_api.py (actions `promote`, `stop`; exact key sets, 16 KiB, same auth as preparation); operator `configure`/`activate`/`stop` stay on `pilot-scope/configure|status` (engine_approval_api.py:122-140), which dispatch by stored contract. `preparation` view gains `{commercial:{eligible,state}}` only. Dashboard: new route `.../accounts/[accountId]/commercial/route.ts`, `lib/onlyfans/commercial-scope.ts` (parse/validate), `Preparation.tsx` shows "Promote to commercial (does not start replies)"; `plan-c-backend.ts` `scope()` parser accepts commercial config and exposes `contractVersion`+`status`.

## 3. Singleton / scope_id=1 / two-fan assumptions to change

- pilot_scope.py :86-87, :99-100 (legacy validate, keep for legacy only), :282, :539-540, :586, :557 (removed-fans check generalises), :637 (loop over audience), :811-815,:847,:857,:874 (stopped-backfill evidence carries fans+product: copy only for legacy), :919-951 (trial exception: scope_id==1 only).
- model_transport/pilot_supervised.py :169,:176,:197,:488,:543,:551 (fans/product), pilot_draft.py :130-143 (TWIN_PLAN_C_FANS/ITEM env: legacy draft only).
- fans_api_adapter/real_transport.py :142,:167,:184 (`allowed_chats == config['fans']`: still valid, audience), events.py :59-71,:395,:609-614, backfill.py :52-62.
- conversation_engine/journey.py :83-84 (any row -> scoped config; must exclude preparation-only), :225-250 (`config['product']`), :288-325 (`s.get(OfEnginePilotScope,1)`; **`scoped=False` for other accounts returns the lenient legacy history coverage**, must become per-account strict), writer_context.py :169-176 (`active_profile` uses legacy `validate`; dispatch by contract), engine.py :147-160 (`_price_information` product), commerce.py :217-225, :377-383, :413, :440 (product/fan checks: raise `commercial_product_contract_unavailable` for commercial rows until Step 3), approval.py :165-216, :412, :688-708, model_ledger.py :118-135, pilot_ingress.py :30,:83, operator_v2.py :109-112, :197-206 (**`s.get(OfEnginePilotScope,1)` makes every other account NotFound**), :228,:292,:390,:460,:806,:964,:1098, plan_c_worker.py :65-71, monitor_incident_recovery.py :77-107 and ops/plan-c-supervision/* (leave legacy-bound, add test they refuse commercial rows).
- moxy_replacement/routing.py :127-129 (`scope_id=1`: change to any scope row -> SELECTED, so legacy bridge never writes for prepared/commercial accounts).
- preparation.py :132-147 (`current`, `assert_send_allowed`), fans_api_adapter/vault_sync.py :144-160 and intake.py :119 (guard type checks accept commercial-configured).
- onlyfans_runtime/store.py :207, text_delivery.py :273-275, :583-588, :805, :1026, launch_operations.py :115-150.

## 4. Risks and guards

| risk | guard / test |
|---|---|
| Commercial row treated as "no scope" by a stale `scope_id==1` read and sent unscoped | grep-style test fails build on `scope_id == 1` / `get(OfEnginePilotScope, 1)` outside an allow-list; per-account lookup test with legacy row + two commercial accounts |
| Activation without promote/launch decision (import, preparation worker, `resume`, policy enable) | DB guard clause 2; `assert_send_allowed` keeps refusing preparation; commercial refused for `enabled` policy unless `authorize(config,actor,'activate')`; `resume` refused when scope not active; test each entrypoint |
| Wrong-account/audience leakage (same fan ID across creators) | every query keyed `(account_id,fan_id)`; two-account same-fan test through intake, control, approval, ledger |
| Counter reset / fresh budget | UPDATE in place; guard monotonic + limits >= counters; promote test compares row before/after (scope_id, binding, created_at, counters, last_attempt_json) |
| Product approval vs runtime authority conflated | separate fields/commands; test: `configure` by agency actor refused, `activate` by agency actor refused, `promote` by operator-only actor refused |
| Silent downgrade / delete+recreate | guard clause 1 and DELETE refusal; test raw SQL attempts |
| Trial exception or legacy 2-fan/$3 rule leaking to commercial | `scope_id==1` gating; legacy suites unchanged |
| Rollback to 0133 code | old code fails closed (see principle); migration downgrade refuses with commercial rows |
| Emergency stop leaves scope active | `stop_scope_in_session` same transaction; test stop then worker `validate()` raises |
| Commercial text-only used as a back door for sales | full mode activation refused until Step 3 `catalogue_ready()` |

## 5. Build list (small commits, failing test first)

1. **Contract validators + dispatch.** Test `service/tests/test_commercial_scope_contract.py` (pure: legacy/preparation/commercial validate, fingerprints, 2-fan rule only legacy, audience cap, shape of catalogue, lineage). Adds `conversation_engine/scope_contract.py`.
2. **Migration 0134 + guard + schema pin.** Test `service/tests/test_commercial_scope_guard_pg.py` (raw SQL: legal promote, illegal promote variants, active only commercial, no downgrade, row 1 frozen, counters/limits, downgrade refusal, hash in schema.py) and update `test_onlyfans_migration_pg.py`/`test_commercial_preparation_pg.py::test_database_guard_*`.
3. **Per-account `current/require/configure/describe` + `assert_send_allowed` kinds.** Test `test_commercial_scope_lookup_pg.py` (legacy row 1 + two accounts + same fan ids; preparation still returns None; damaged config fails closed).
4. **`promote` command + `/commercial` backend route.** Test `test_commercial_scope_promote_pg.py` (idempotent replay, actor classes, evidence copy, counters equal, nothing active, delivery still refused).
5. **`configure`/`activate`/`stop` for commercial (text-only first), `stop_scope_in_session`, `resume` guard, emergency stop.** Test `test_commercial_scope_lifecycle_pg.py` (every activation entrypoint, stop->validate, full mode refused with `commercial_product_contract_unavailable`).
6. **Singleton removals** (list in section 3; journey coverage strict, operator_v2 `_scope`, routing.py, trial exception gating, worker env per account). Test `test_commercial_scope_isolation_pg.py` (ten accounts, repeated fan ids, one held account does not stop others) + allow-list grep test.
7. **Dashboard/BFF.** `tests/contracts/commercial-scope.test.ts`, route test, `tests/components/preparation-failure.test.tsx` unchanged green; actual BFF -> ASGI -> Postgres run as in `check_preparation_dashboard.py`.

Must stay green throughout (existing): test_plan_c_scope_pg, test_planc_writer_fence, test_commercial_preparation_pg, test_preparation_retry_state_pg, test_plan_c_supervised_pg, test_plan_c_stopped_backfill_pg, test_plan_c_text_activation_pg, test_plan_c_text_stop_resume_pg, test_plan_c_trial_takeover_exception_pg, test_plan_c_pilot_lifetime_budget_pg, test_engine_approval_api_pg, test_plan_c_qualification_pg, test_plan_c_sales_journey_pg, test_plan_c_nonempty_history_pg, test_plan_c_u3_readiness_pg, test_plan_c_model_retry_due_pg, test_plan_c_grant_lifecycle_pg, test_commercial_provider_family_pg, test_onlyfans_migration_pg, test_moxy_pilot_pg.

## 6. Decisions needed from the owner (with recommendation)

- **D1 Legacy row 1 (of-323358988 trial).** Recommend: leave frozen as legacy in Step 2; revisit a pilot->commercial upgrade only after the FansAPI trial ends (~13 Nov) so the running trial is never touched.
- **D2 Audience size.** Recommend explicit list, cap 100 per creator, changed only while stopped; "all fans" is not offered. Non-audience fans stay recorded-as-outside evidence (Step 4 decides richer staff coexistence).
- **D3 Separation of duties.** Recommend allow one person to configure and activate, but `promote` by agency and `activate` by the named operator are distinct audited commands; stricter two-person rule only if you want it.
- **D4 Full-mode activation before Step 3.** Recommend refuse (text-only commercial scopes only) so no account can sell through a half-built product contract.
