# NEEDS-JOIN (A6, real model invocation) for A5

Branch `claude/engine-model-20261107`, based on `claude/engine-defects-20261107` @ 5edb62a. Everything is default OFF.

## 1. Migration chain (the one thing that will not apply as-is)
- New revision `0122_engine_model_calls`, file `service/db/migrations/versions/20261107_0900_engine_model_calls.py`.
- It currently has `down_revision = '0120_engine_generations'` because 0121 does not exist on this base. When the chain is
  0119 -> 0120 (generations) -> 0121 (fans intake) -> 0122, change that ONE line to the 0121 revision id.
- `service/db/schema.py` (conflict-prone): added `ENGINE_MODEL_CALLS = "0122_engine_model_calls"`, `HEAD = ENGINE_MODEL_CALLS`,
  `REVISION_RANK[ENGINE_MODEL_CALLS] = 122`, and a `check_schema` block for `expected_rank >= 122` (guard trigger digest + row
  security for `of_engine_model_call`). Keep whichever HEAD/rank lines A5 produces, then make HEAD the 0122 constant, and keep
  the new block. The guard-function digest was computed from the migration text; if the migration text is edited, recompute it.
- `service/db/onlyfans_engine_models.py`: appended `OfEngineModelCall` (+ `Date` in the sqlalchemy import).

## 2. engine.py hunks (all in `service/conversation_engine/engine.py`)
- `EngineService.__init__` and `plan()` accept a `ModelRuntime` besides the two fixture models.
- `plan()`: for a `ModelRuntime`, adds `offer_grants` and `verified_purchases` to the snapshot (fixture snapshots unchanged),
  builds a `ModelTurn` and calls `self.model.propose(turn)` instead of `self.model.generate(snapshot)`.
- New: `_AlreadyPlanned`, `_offer_grants`, `_verified_purchases`, `EngineService._model_recheck`, `EngineService._handover_model`,
  and a guard `request is with a human` for `handover_pending/accepted` requests with no decision yet.
- `_model_recheck` deliberately does NOT test `policy.transport_kind`; the final transaction in `plan()` still does. If A5
  changes the transport gate in `plan()`, nothing needs mirroring in `_model_recheck`.
- `worker.py`, `model_contract.py`, `claims.py`, `onlyfans_runtime/text_delivery.py` are untouched.

## 3. Network library boundary
- `httpx` lives only in `service/model_transport/openrouter.py` (outside `conversation_engine/`).
  `tests/conversation_engine/test_model_units.py::test_only_the_transport_package_imports_a_network_library` pins that.
- The older `test_import_boundary.py` still fails on `fans_api_adapter/https_transport.py` (importing httpx). That failure
  exists on the base (REVIEW-EVIDENCE.md) and is not touched here; A5 owns it.

## 4. Wiring left for A5/entry point
- Build the runtime once per worker: `model_transport.build_model_runtime(engine_db, ModelConfig.from_env(os.environ), profiles)`
  (returns None unless `ENGINE_MODEL_ENABLED=1`), then `EngineService(store, runtime)`.
- `profiles` is any object with `.get(account_id) -> CreatorProfile | None` (`writer_context.py`: `StaticCreatorProfiles`,
  `JsonFileCreatorProfiles`, `CreatorProfile.from_facts(...)` for the shared creator-fact keys). No production source is wired.
- The key comes from `ENGINE_MODEL_KEY_FILE` (0600, owned by the process) or the env var NAMED by `ENGINE_MODEL_KEY_ENV`.
- `service/` is copied whole by the Dockerfile and `httpx==0.28.1` is already in `requirements.txt`; no packaging change needed.

## 5. Prompt provider registry (for Plan C)
- `service/conversation_engine/prompt_registry.py`: `register_prompt_provider(name)` decorator, `get_prompt_provider(name)`,
  `available_prompt_providers()`. Selected by `ModelConfig.prompt_provider` / `ENGINE_MODEL_PROMPT_PROVIDER` (default `a-original`).
- Every module named `conversation_engine/prompt_provider_*.py` is auto-imported on first registry use, so a second provider is
  ONE new file (class with `name` + `render(PromptContext) -> RenderedPrompt`, decorated with `@register_prompt_provider('...')`).
  `test_registry_default_and_second_provider_plugs_in` shows the pattern. `test_registry_discovers_provider_modules_by_filename_only`
  asserts the provider-file set; Plan C updates that single expected set when it adds its file.
- The only provider on this branch is `prompt_provider_a_original.py` (from FINAL-TWELVE-RULE-SPECIFICATION.txt). No Build B / FASTT text.
