"""Model invocation configuration. Everything defaults OFF; the engine never reads os.environ itself.

`ModelConfig.from_env(environ)` is the application-configuration entry point (see model_transport/wiring.py).
Secrets are never part of the config: the real client takes a key *file path* (mode 0600) or the *name* of an
environment variable.

Plan C pilot rules enforced here, not left to prose:
* ONE primary model plus at most ONE explicitly configured fallback (Ronan decision 2026-11-07; Jesse decision not yet
  recorded). The fallback is OFF by default and needs its flag, id and its own prices, else startup refuses. It is used
  only after an AVAILABILITY failure of the primary (see model_runtime), never after a refusal, an empty completion or a
  validator rejection. Comma-separated lists and auto-routing model ids are rejected, never ignored silently.
* Every spending figure is a conservative UPPER BOUND computed from configured limits: max input tokens, max output
  tokens, the PER-MODEL token prices and the bounded number of attempts. There is no fixed "turn estimate".
* Input and output sizes are bounded (`max_input_tokens`, `max_tokens`, `max_output_bytes`).
"""
from dataclasses import dataclass
from hashlib import sha256
import json


# The Plan C pilot model (Ronan, 2026-11-07). Used as the documented example; never selected implicitly.
DEFAULT_PILOT_MODEL = 'deepseek/deepseek-v4.1-flash'
DEFAULT_PILOT_FALLBACK_MODEL = 'x-ai/grok-4.1-fast'   # the owner-chosen fallback; OFF unless explicitly enabled


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


# A BPE/SentencePiece token covers at least one byte, so the UTF-8 byte count of the text is a hard upper bound on
# its token count. Chat framing adds a few tokens per message; the constants below are generous.
_MESSAGE_OVERHEAD_TOKENS = 12
_REQUEST_OVERHEAD_TOKENS = 16


def input_token_upper_bound(messages):
    """Conservative upper bound on the prompt tokens of a chat request (never an estimate of the real count)."""
    total = _REQUEST_OVERHEAD_TOKENS
    for message in messages:
        total += _MESSAGE_OVERHEAD_TOKENS + len(str(message.get('content', '')).encode('utf-8'))
    return total


@dataclass(frozen=True)
class ModelConfig:
    enabled: bool = False                          # ENGINE_MODEL_ENABLED
    provider: str = 'openrouter'                   # ENGINE_MODEL_PROVIDER
    primary_model: str = ''                        # ENGINE_MODEL_PRIMARY (the ONE pilot model, e.g. DEFAULT_PILOT_MODEL)
    fallback_enabled: bool = False                 # ENGINE_MODEL_FALLBACK_ENABLED (OFF by default)
    fallback_model: str = ''                       # ENGINE_MODEL_FALLBACK (e.g. DEFAULT_PILOT_FALLBACK_MODEL); at most ONE
    fallback_prompt_microusd_per_mtok: int = 0     # ENGINE_MODEL_FALLBACK_PROMPT_MICROUSD_PER_MTOK (required when enabled)
    fallback_completion_microusd_per_mtok: int = 0  # ENGINE_MODEL_FALLBACK_COMPLETION_MICROUSD_PER_MTOK (required when enabled)
    allowed_models: tuple = ()                     # ENGINE_MODEL_ALLOWED (explicit allow-list; empty = nothing allowed)
    enabled_accounts: frozenset = frozenset()      # ENGINE_MODEL_ACCOUNTS (accounts allowed to call a model)
    prompt_provider: str = 'a-original'           # ENGINE_MODEL_PROMPT_PROVIDER (see prompt_registry.py)
    key_file: str = ''                             # ENGINE_MODEL_KEY_FILE (0600 file holding the key)
    key_env: str = ''                              # ENGINE_MODEL_KEY_ENV (NAME of an env var holding the key)
    base_url: str = 'https://openrouter.ai/api/v1'  # ENGINE_MODEL_BASE_URL
    timeout_seconds: float = 45.0                  # ENGINE_MODEL_TIMEOUT_SECONDS
    max_input_tokens: int = 16000                  # ENGINE_MODEL_MAX_INPUT_TOKENS (a bigger prompt is never sent)
    max_tokens: int = 700                          # ENGINE_MODEL_MAX_TOKENS (output bound sent to the provider)
    max_output_bytes: int = 8000                   # ENGINE_MODEL_MAX_OUTPUT_BYTES (a longer answer is rejected)
    temperature: float = 0.85
    # --- budget (micro-USD: 1_000_000 = 1 USD). A zero cap means "spend nothing".
    daily_cap_microusd: int = 0                    # ENGINE_MODEL_DAILY_CAP_MICROUSD
    creator_cap_microusd: int = 0                  # ENGINE_MODEL_CREATOR_CAP_MICROUSD (per account per UTC day)
    # Verified provider prices in micro-USD per million tokens. REQUIRED when enabled: they are the basis of the
    # reservation upper bound (and the cost fallback when a provider omits one).
    prompt_microusd_per_mtok: int = 0              # ENGINE_MODEL_PROMPT_MICROUSD_PER_MTOK
    completion_microusd_per_mtok: int = 0          # ENGINE_MODEL_COMPLETION_MICROUSD_PER_MTOK
    # --- lease and attempts
    lease_seconds: int = 90                        # ENGINE_MODEL_LEASE_SECONDS
    transport_retry_seconds: int = 30              # a transport failure older than this no longer blocks a retry
    max_calls_per_generation: int = 2              # ENGINE_MODEL_MAX_ATTEMPTS: TOTAL invocations for one request generation
    handover_target: str = 'operator'              # who receives a request the model could not write

    def __post_init__(self):
        if (type(self.enabled) is not bool or type(self.allowed_models) is not tuple
                or type(self.fallback_enabled) is not bool):
            raise ValueError('invalid model config')
        for name in ('daily_cap_microusd', 'creator_cap_microusd', 'lease_seconds', 'max_calls_per_generation',
                     'max_tokens', 'transport_retry_seconds', 'prompt_microusd_per_mtok',
                     'completion_microusd_per_mtok', 'max_input_tokens', 'max_output_bytes',
                     'fallback_prompt_microusd_per_mtok', 'fallback_completion_microusd_per_mtok'):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f'{name} must be a non-negative integer')
        if self.lease_seconds < 1 or self.max_calls_per_generation < 1 or self.max_calls_per_generation > 3:
            raise ValueError('invalid lease or attempt bound (total attempts must be 1 to 3)')
        if self.key_file and self.key_env:
            raise ValueError('configure a key file or a key env var name, not both')
        if type(self.enabled_accounts) is not frozenset:
            raise ValueError('enabled_accounts must be a frozenset')
        model = self.primary_model

        def explicit(value):
            return not (',' in value or value.startswith('openrouter/auto') or value.endswith(':auto')
                        or any(c.isspace() for c in value))
        if not explicit(model) or not explicit(self.fallback_model):
            raise ValueError('models must be single explicit ids (no fallback list or auto routing)')
        if self.enabled:
            if not model:
                raise ValueError('a primary model is required when model invocation is enabled')
            if not (self.max_input_tokens and self.max_tokens and self.max_output_bytes):
                raise ValueError('input and output bounds are required when model invocation is enabled')
            if not (self.prompt_microusd_per_mtok and self.completion_microusd_per_mtok):
                raise ValueError('verified per-token prices are required when model invocation is enabled')
            if self.fallback_enabled:
                if not self.fallback_model or self.fallback_model == model:
                    raise ValueError('an enabled fallback needs its own model id, different from the primary')
                if not (self.fallback_prompt_microusd_per_mtok and self.fallback_completion_microusd_per_mtok):
                    raise ValueError('verified per-token prices for the fallback model are required when the fallback is enabled')

    @classmethod
    def from_env(cls, environ):
        if environ.get('ENGINE_MODEL_ALTERNATE'):
            raise ValueError('ENGINE_MODEL_ALTERNATE is gone: use ENGINE_MODEL_FALLBACK_ENABLED, ENGINE_MODEL_FALLBACK and its prices')
        return cls(
            enabled=_flag(environ, 'ENGINE_MODEL_ENABLED'),
            provider=environ.get('ENGINE_MODEL_PROVIDER', 'openrouter'),
            primary_model=environ.get('ENGINE_MODEL_PRIMARY', ''),
            fallback_enabled=_flag(environ, 'ENGINE_MODEL_FALLBACK_ENABLED'),
            fallback_model=environ.get('ENGINE_MODEL_FALLBACK', ''),
            fallback_prompt_microusd_per_mtok=_int(environ, 'ENGINE_MODEL_FALLBACK_PROMPT_MICROUSD_PER_MTOK', 0),
            fallback_completion_microusd_per_mtok=_int(environ, 'ENGINE_MODEL_FALLBACK_COMPLETION_MICROUSD_PER_MTOK', 0),
            allowed_models=_list(environ, 'ENGINE_MODEL_ALLOWED'),
            enabled_accounts=frozenset(_list(environ, 'ENGINE_MODEL_ACCOUNTS')),
            prompt_provider=environ.get('ENGINE_MODEL_PROMPT_PROVIDER', 'a-original'),
            key_file=environ.get('ENGINE_MODEL_KEY_FILE', ''),
            key_env=environ.get('ENGINE_MODEL_KEY_ENV', ''),
            base_url=environ.get('ENGINE_MODEL_BASE_URL', 'https://openrouter.ai/api/v1'),
            timeout_seconds=float(environ.get('ENGINE_MODEL_TIMEOUT_SECONDS', 45)),
            max_input_tokens=_int(environ, 'ENGINE_MODEL_MAX_INPUT_TOKENS', 16000),
            max_tokens=_int(environ, 'ENGINE_MODEL_MAX_TOKENS', 700),
            max_output_bytes=_int(environ, 'ENGINE_MODEL_MAX_OUTPUT_BYTES', 8000),
            daily_cap_microusd=_int(environ, 'ENGINE_MODEL_DAILY_CAP_MICROUSD', 0),
            creator_cap_microusd=_int(environ, 'ENGINE_MODEL_CREATOR_CAP_MICROUSD', 0),
            prompt_microusd_per_mtok=_int(environ, 'ENGINE_MODEL_PROMPT_MICROUSD_PER_MTOK', 0),
            completion_microusd_per_mtok=_int(environ, 'ENGINE_MODEL_COMPLETION_MICROUSD_PER_MTOK', 0),
            lease_seconds=_int(environ, 'ENGINE_MODEL_LEASE_SECONDS', 90),
            max_calls_per_generation=_int(environ, 'ENGINE_MODEL_MAX_ATTEMPTS', 2),
            handover_target=environ.get('ENGINE_MODEL_HANDOVER_TARGET', 'operator'),
        )

    # ---- spend bounds -----------------------------------------------------------
    def models(self):
        """The models this config may ever call: the primary, then the fallback only when it is enabled."""
        return (self.primary_model,) + ((self.fallback_model,) if self.fallback_enabled else ())

    def prices_for(self, model):
        """(prompt, completion) micro-USD per million tokens for one of this config's models."""
        if self.fallback_enabled and model == self.fallback_model:
            return self.fallback_prompt_microusd_per_mtok, self.fallback_completion_microusd_per_mtok
        return self.prompt_microusd_per_mtok, self.completion_microusd_per_mtok

    def call_bound_microusd(self, model=None):
        """Upper bound for ONE invocation of `model` (default: the primary): every allowed input token and every
        allowed output token at that model's list price."""
        prompt, completion = self.prices_for(model or self.primary_model)
        total = self.max_input_tokens * prompt + self.max_tokens * completion
        return -(-total // 1_000_000)

    def max_call_bound_microusd(self):
        """The largest per-call bound over every model this config can call (used to size the retry allowance)."""
        return max(self.call_bound_microusd(m) for m in self.models())

    def generation_bound_microusd(self):
        """Upper bound for one request generation: every permitted attempt at the largest per-call bound."""
        return self.max_call_bound_microusd() * self.max_calls_per_generation

    def version(self):
        """Short stable identity of the non-secret settings that shape a call (recorded with each call)."""
        shape = {'provider': self.provider, 'model': self.primary_model, 'prompt_provider': self.prompt_provider,
                 'max_input_tokens': self.max_input_tokens, 'max_tokens': self.max_tokens,
                 'max_output_bytes': self.max_output_bytes, 'temperature': self.temperature,
                 'prices': [self.prompt_microusd_per_mtok, self.completion_microusd_per_mtok],
                 'fallback': [self.fallback_model, self.fallback_prompt_microusd_per_mtok,
                              self.fallback_completion_microusd_per_mtok] if self.fallback_enabled else None,
                 'attempts': self.max_calls_per_generation}
        return 'cfg-' + sha256(json.dumps(shape, sort_keys=True).encode()).hexdigest()[:12]

    def chain(self):
        """Ordered (chain_step, model, prefixed): the primary, ONE corrected regeneration on the primary, then the
        optional fallback. The runtime reaches 'fallback' only after an availability failure of the primary and never
        after a refusal, an empty completion or a validator rejection (those are human work). Total attempts are
        bounded by `max_calls_per_generation`.
        """
        steps = [('primary', self.primary_model, False)] + [('retry', self.primary_model, False)] * (self.max_calls_per_generation - 1)
        if self.fallback_enabled:
            steps.append(('fallback', self.fallback_model, False))
        return steps
