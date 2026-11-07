"""Network-facing model transports for the conversation engine (kept out of conversation_engine/ on purpose).

`conversation_engine` imports no network library (enforced by its import-boundary test); this package is the one
place a provider client lives, and only `build_model_runtime` wires it in. Everything defaults OFF.
"""


def build_model_runtime(engine_db, config, profiles, *, environ=None, transport=None, examples=None, prompts=None):
    """Return a ModelRuntime, or None when ModelConfig.enabled is false (the default)."""
    if not config.enabled:
        return None
    from conversation_engine.model_runtime import ModelRuntime
    if config.provider != 'openrouter':
        raise ValueError('unsupported model provider')
    from .openrouter import OpenRouterClient
    client = OpenRouterClient(base_url=config.base_url, key_file=config.key_file, key_env=config.key_env,
                              environ=environ, transport=transport)
    return ModelRuntime(engine_db, client, config, profiles, examples=examples, prompts=prompts)
