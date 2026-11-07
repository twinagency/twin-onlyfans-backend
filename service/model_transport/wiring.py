"""Select the real model runtime through normal application configuration (environment variables). Default OFF.

    ENGINE_MODEL_ENABLED=1                      turns the runtime on (anything else: build_* return None)
    ENGINE_MODEL_PRIMARY / ENGINE_MODEL_ALLOWED the ONE model, e.g. deepseek/deepseek-v4.1-flash 
    ENGINE_MODEL_FALLBACK_ENABLED=1             OFF by default. Needs ENGINE_MODEL_FALLBACK (e.g. x-ai/grok-4.1-fast) and its own
                                                ENGINE_MODEL_FALLBACK_PROMPT_MICROUSD_PER_MTOK / _COMPLETION_ prices, else startup
                                                refuses. Used only after an AVAILABILITY failure of the primary; never after a
                                                refusal, empty completion or rejected draft. (ENGINE_MODEL_ALTERNATE is refused.)
    ENGINE_MODEL_ACCOUNTS                       account ids allowed to call a model
    ENGINE_MODEL_KEY_FILE | ENGINE_MODEL_KEY_ENV  0600 key file or NAME of the env var holding the key
    ENGINE_MODEL_PROMPT_MICROUSD_PER_MTOK, ENGINE_MODEL_COMPLETION_MICROUSD_PER_MTOK   verified prices: REQUIRED, startup
                                                refuses (ValueError) when enabled and either is unset; never fetched from the network
    ENGINE_MODEL_MAX_INPUT_TOKENS, ENGINE_MODEL_MAX_TOKENS, ENGINE_MODEL_MAX_OUTPUT_BYTES, ENGINE_MODEL_MAX_ATTEMPTS
    ENGINE_MODEL_DAILY_CAP_MICROUSD, ENGINE_MODEL_CREATOR_CAP_MICROUSD
    ENGINE_MODEL_PROFILES_FILE                  approved creator profiles (JSON: {account_id: profile}); REQUIRED
    ENGINE_MODEL_EXAMPLES_DIR                   optional directory of approved voice examples (<account_id>.jsonl)

The profile file is the approved creator-profile source: structured creator facts, documented boundaries (`limits`),
the approved identity wording (`role_statement`) and the commercial permission (`offers_permitted`,
`approved_item_ids`). A missing, unreadable or invalid entry is a missing input: that account's requests go to a human.
No monkeypatch, no ad hoc SQL, no synthetic stand-in: `build_engine_service` returns an `EngineService` around the real
`ModelRuntime`, which `EngineService` accepts by type.
"""
import os


def build_runtime_from_env(engine_db, environ=None, *, transport=None):
    """The configured ModelRuntime, or None when ENGINE_MODEL_ENABLED is not set (the default)."""
    from conversation_engine.model_config import ModelConfig
    from conversation_engine.writer_context import JsonFileCreatorProfiles
    from . import build_model_runtime
    environ = os.environ if environ is None else environ
    config = ModelConfig.from_env(environ)
    if not config.enabled:
        return None
    path = environ.get('ENGINE_MODEL_PROFILES_FILE', '')
    if not path:
        raise ValueError('ENGINE_MODEL_PROFILES_FILE is required when the model runtime is enabled')
    examples = None
    directory = environ.get('ENGINE_MODEL_EXAMPLES_DIR', '')
    if directory:
        from conversation_engine.writer_examples import JsonlExamples
        examples = JsonlExamples(directory)
    return build_model_runtime(engine_db, config, JsonFileCreatorProfiles(path), environ=environ,
                               transport=transport, examples=examples)


def build_engine_service(store, environ=None, *, transport=None, draft_only=False):
    """EngineService around the real ModelRuntime selected by configuration, or None when it is not enabled."""
    runtime = build_runtime_from_env(store.engine, environ, transport=transport)
    if runtime is None:
        return None
    from conversation_engine.engine import EngineService
    return EngineService(store, runtime, draft_only=draft_only)
