"""Registry of writer PromptProviders: name -> provider class.

Selecting a provider is configuration only (ModelConfig.prompt_provider / ENGINE_MODEL_PROMPT_PROVIDER).

ADDING A PROVIDER IS A ONE-FILE CHANGE. Create `conversation_engine/prompt_provider_<anything>.py`, subclass nothing,
implement the `PromptProvider` protocol (`name` attribute and `render(ctx) -> RenderedPrompt`, see writer_context.py)
and decorate the class:

    from .prompt_registry import register_prompt_provider

    @register_prompt_provider('my-provider')
    class MyProvider:
        name = 'my-provider.v1'
        def render(self, ctx): ...

Every module in this package whose name starts with `prompt_provider_` is imported the first time the registry is
used, so no other file needs editing. The default provider on this branch is 'a-original'
(prompt_provider_a_original.py, built from FINAL-TWELVE-RULE-SPECIFICATION.txt).
A provider receives the same PromptContext and can ignore fields it does not need; it must keep its output within
its own budget and must not put prices, grant ids it was not given, or instructions to send into the prompt.
"""
import importlib
import pkgutil

DEFAULT_PROVIDER = 'a-original'
_PROVIDERS = {}
_loaded = False


def register_prompt_provider(name):
    if type(name) is not str or not name:
        raise ValueError('provider name required')

    def decorate(cls):
        existing = _PROVIDERS.get(name)
        if existing is not None and existing is not cls and (existing.__module__, existing.__qualname__) != (cls.__module__, cls.__qualname__):
            raise ValueError(f'prompt provider {name!r} is already registered')
        _PROVIDERS[name] = cls
        return cls
    return decorate


def _load_builtin():
    global _loaded
    if _loaded:
        return
    _loaded = True
    package = importlib.import_module(__package__)
    for info in pkgutil.iter_modules(package.__path__):
        if info.name.startswith('prompt_provider_'):
            importlib.import_module(f'{__package__}.{info.name}')


def available_prompt_providers():
    _load_builtin()
    return tuple(sorted(_PROVIDERS))


def get_prompt_provider(name=DEFAULT_PROVIDER, **kwargs):
    """Instantiate a registered provider by name. Unknown names raise ValueError (no silent default)."""
    _load_builtin()
    cls = _PROVIDERS.get(name)
    if cls is None:
        raise ValueError(f'unknown prompt provider {name!r}; available: {", ".join(sorted(_PROVIDERS))}')
    return cls(**kwargs)
