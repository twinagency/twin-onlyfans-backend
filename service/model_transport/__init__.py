"""Network-facing model transports for the conversation engine (kept out of conversation_engine/ on purpose).

`conversation_engine` imports no network library (enforced by its import-boundary test); this package is the one
place a provider client lives, and only `build_model_runtime` wires it in. Everything defaults OFF.
"""


def build_model_runtime(engine_db, config, profiles, *, environ=None, transport=None, examples=None, prompts=None):
    """Return a ModelRuntime, or None when ModelConfig.enabled is false (the default)."""
    if not config.enabled:
        return None
    from conversation_engine.model_runtime import ModelRuntime
    from conversation_engine.writer_context import set_active_profiles
    if config.provider != 'openrouter':
        raise ValueError('unsupported model provider')
    from .openrouter import OpenRouterClient
    prices = {model: config.prices_for(model) for model in config.models()}
    client = OpenRouterClient(base_url=config.base_url, key_file=config.key_file, key_env=config.key_env,
                              environ=environ, transport=transport, upstream_provider=config.upstream_provider,
                              price_caps=prices)
    set_active_profiles(profiles)  # the journey rules read the same approved identity wording
    return ModelRuntime(engine_db, client, config, profiles, examples=examples, prompts=prompts)
