"""Model invocation configuration. Everything defaults OFF; the engine never reads os.environ itself.

`ModelConfig.from_env(environ)` is a convenience for the worker entry point. Secrets are never part of
the config: the real client takes a key *file path* (mode 0600) or the *name* of an environment variable.
"""
from dataclasses import dataclass, field


def _int(environ, name, default):
    raw = environ.get(name)
    if raw is None or raw == '':
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f'{name} must be an integer') from None


def _flag(environ, name):
    return str(environ.get(name, '0')).strip().lower() in {'1', 'true', 'yes', 'on'}


def _list(environ, name):
    return tuple(part.strip() for part in str(environ.get(name, '')).split(',') if part.strip())


@dataclass(frozen=True)
class ModelConfig:
    enabled: bool = False                          # ENGINE_MODEL_ENABLED
    provider: str = 'openrouter'                   # ENGINE_MODEL_PROVIDER
    primary_model: str = ''                        # ENGINE_MODEL_PRIMARY
    alternate_model: str = ''                      # ENGINE_MODEL_ALTERNATE (refusal fallback, optional)
    allowed_models: tuple = ()                     # ENGINE_MODEL_ALLOWED (explicit allow-list; empty = nothing allowed)
    enabled_accounts: frozenset = frozenset()      # ENGINE_MODEL_ACCOUNTS (accounts allowed to call a model)
    prompt_provider: str = 'a-original'           # ENGINE_MODEL_PROMPT_PROVIDER (see prompt_registry.py)
    key_file: str = ''                             # ENGINE_MODEL_KEY_FILE (0600 file holding the key)
    key_env: str = ''                              # ENGINE_MODEL_KEY_ENV (NAME of an env var holding the key)
    base_url: str = 'https://openrouter.ai/api/v1'  # ENGINE_MODEL_BASE_URL
    timeout_seconds: float = 45.0                  # ENGINE_MODEL_TIMEOUT_SECONDS
    max_tokens: int = 700                          # ENGINE_MODEL_MAX_TOKENS
    temperature: float = 0.85
    # --- budget (micro-USD: 1_000_000 = 1 USD). A zero cap means "spend nothing".
    daily_cap_microusd: int = 0                    # ENGINE_MODEL_DAILY_CAP_MICROUSD
    creator_cap_microusd: int = 0                  # ENGINE_MODEL_CREATOR_CAP_MICROUSD (per account per UTC day)
    turn_estimate_microusd: int = 5000             # ENGINE_MODEL_TURN_ESTIMATE_MICROUSD reserved per call
    prompt_microusd_per_mtok: int = 0              # used only when the provider omits a cost
    completion_microusd_per_mtok: int = 0
    # --- lease and chain
    lease_seconds: int = 90                        # ENGINE_MODEL_LEASE_SECONDS
    transport_retry_seconds: int = 30              # a transport failure older than this no longer blocks a retry
    max_calls_per_generation: int = 8              # hard bound on invocations for one request generation
    handover_target: str = 'operator'              # who receives a request the model could not write

    def __post_init__(self):
        if type(self.enabled) is not bool or type(self.allowed_models) is not tuple:
            raise ValueError('invalid model config')
        for name in ('daily_cap_microusd', 'creator_cap_microusd', 'turn_estimate_microusd', 'lease_seconds',
                     'max_calls_per_generation', 'max_tokens', 'transport_retry_seconds',
                     'prompt_microusd_per_mtok', 'completion_microusd_per_mtok'):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f'{name} must be a non-negative integer')
        if self.lease_seconds < 1 or self.max_calls_per_generation < 1 or self.max_calls_per_generation > 20:
            raise ValueError('invalid lease or call bound')
        if self.key_file and self.key_env:
            raise ValueError('configure a key file or a key env var name, not both')
        if type(self.enabled_accounts) is not frozenset:
            raise ValueError('enabled_accounts must be a frozenset')

    @classmethod
    def from_env(cls, environ):
        return cls(
            enabled=_flag(environ, 'ENGINE_MODEL_ENABLED'),
            provider=environ.get('ENGINE_MODEL_PROVIDER', 'openrouter'),
            primary_model=environ.get('ENGINE_MODEL_PRIMARY', ''),
            alternate_model=environ.get('ENGINE_MODEL_ALTERNATE', ''),
            allowed_models=_list(environ, 'ENGINE_MODEL_ALLOWED'),
            enabled_accounts=frozenset(_list(environ, 'ENGINE_MODEL_ACCOUNTS')),
            prompt_provider=environ.get('ENGINE_MODEL_PROMPT_PROVIDER', 'a-original'),
            key_file=environ.get('ENGINE_MODEL_KEY_FILE', ''),
            key_env=environ.get('ENGINE_MODEL_KEY_ENV', ''),
            base_url=environ.get('ENGINE_MODEL_BASE_URL', 'https://openrouter.ai/api/v1'),
            timeout_seconds=float(environ.get('ENGINE_MODEL_TIMEOUT_SECONDS', 45)),
            max_tokens=_int(environ, 'ENGINE_MODEL_MAX_TOKENS', 700),
            daily_cap_microusd=_int(environ, 'ENGINE_MODEL_DAILY_CAP_MICROUSD', 0),
            creator_cap_microusd=_int(environ, 'ENGINE_MODEL_CREATOR_CAP_MICROUSD', 0),
            turn_estimate_microusd=_int(environ, 'ENGINE_MODEL_TURN_ESTIMATE_MICROUSD', 5000),
            lease_seconds=_int(environ, 'ENGINE_MODEL_LEASE_SECONDS', 90),
            handover_target=environ.get('ENGINE_MODEL_HANDOVER_TARGET', 'operator'),
        )

    def chain(self):
        """Ordered (chain_step, model, prefixed): primary, in-character retry, alternate (if different)."""
        steps = [('primary', self.primary_model, False), ('retry', self.primary_model, True)]
        if self.alternate_model and self.alternate_model != self.primary_model:
            steps.append(('alternate', self.alternate_model, True))
        return steps
