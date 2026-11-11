"""Model client seam for the engine: provider-neutral types, errors and test clients.

This module performs no network I/O and imports no network library. A real
client (service/model_transport/openrouter.py) implements `ChatClient`; the
engine only ever sees this interface. The model never has send or charge
authority: its text becomes an engine proposal that the engine validates and
that the existing delivery authority still has to admit.
"""
from dataclasses import dataclass, field
from typing import Callable, Protocol

from onlyfans_runtime.store import Refused


# ---- request / result ---------------------------------------------------------
@dataclass(frozen=True)
class ChatRequest:
    model: str
    messages: tuple  # tuple of {'role': str, 'content': str}
    max_tokens: int = 700
    temperature: float = 0.85
    json_mode: bool = True
    timeout_seconds: float = 45.0


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_microusd: int | None = None  # None = the provider did not tell us what it charged


@dataclass(frozen=True)
class ChatResult:
    text: str
    usage: Usage = field(default_factory=Usage)
    model: str = ''              # the model id the PROVIDER says answered (may differ from the requested one)
    finish_reason: str | None = None
    provider: str = ''           # upstream provider named in the response, when it names one
    generation_id: str = ''      # provider-issued correlation only; never supplied by an operator
    endpoint_sha256: str = ''    # exact configured endpoint that returned the ID


# ---- errors -------------------------------------------------------------------
class ModelCallError(Exception):
    """Base class. Never carries a secret; `error_class` is stored in the call record."""
    error_class = 'model_error'


class ModelNotSent(ModelCallError):
    """Proven to have failed before any request bytes were sent. No charge is possible."""
    error_class = 'not_sent'


class ModelHTTPStatus(ModelCallError):
    """The provider answered with a non-success status. Providers do not charge for these.

    `detail` is a bounded, sanitized dict (status, provider error code, provider name, short redacted message,
    request id, rate-limit headers) kept as evidence; `retry_after` is the provider's Retry-After in whole seconds
    (0..3600) when it sent one. Neither ever carries a key, prompt or raw body."""
    def __init__(self, status, *, detail=None, retry_after=None):
        super().__init__(f'provider returned HTTP {status}')
        self.status = status
        self.error_class = f'http_{status}'
        self.detail = detail if type(detail) is dict else None
        self.retry_after = retry_after if type(retry_after) is int and 0 <= retry_after <= 3600 else None


def retryable_release(outcome):
    """A released call whose failure is a definitive availability refusal: nothing is in flight, no usage is unknown,
    and the same request may be asked again after its due time (not_sent, HTTP 408/429/5xx). Every other released
    outcome is either a never-wired lease (handled separately) or a provider refusal a retry cannot fix."""
    if outcome == 'not_sent':
        return True
    if type(outcome) is str and outcome.startswith('http_') and outcome[5:].isdigit() and len(outcome) == 8:
        status = int(outcome[5:])
        return status in (408, 429) or 500 <= status <= 599
    return False


def http_refusal(outcome):
    """A released call refused by the provider with a non-retryable HTTP status (400/401/403/404/422...)."""
    return (type(outcome) is str and outcome.startswith('http_') and outcome[5:].isdigit() and len(outcome) == 8
            and not retryable_release(outcome))


class ModelOutcomeUnknown(ModelCallError):
    """The request may have reached the provider (timeout, reset after send). Cost is unknown."""
    error_class = 'unknown_outcome'


class ModelIncompleteResponse(ModelOutcomeUnknown):
    # Cost and output are not trustworthy after a provider-reported error.
    error_class = 'provider_incomplete'

    def __init__(self, message, *, generation_id='', endpoint_sha256=''):
        super().__init__(message)
        self.generation_id, self.endpoint_sha256 = generation_id, endpoint_sha256


class ModelUnavailable(Refused):
    """Every transport attempt failed; the request stays open and may be retried later."""


class ModelBudgetExhausted(Refused):
    """A daily or per-creator cap would be exceeded; nothing was sent."""


class ModelBusy(Refused):
    """Another worker holds the lease for this request generation."""


# ---- interface ----------------------------------------------------------------
class ChatClient(Protocol):
    """Transport to one provider. Must not retry internally and must not log message content."""
    provider: str

    def chat(self, request: ChatRequest) -> ChatResult: ...


class FixtureChatClient:
    """Scripted offline client. `script` is a list of str | ChatResult | Exception | callable(request)->those.

    The last entry repeats when the script is exhausted (so a refusing model can refuse forever).
    """
    provider = 'fixture'

    def __init__(self, script, *, cost_microusd=1000):
        if not isinstance(script, (list, tuple)) or not script:
            raise ValueError('fixture chat script required')
        self._script = list(script)
        self._cost = cost_microusd
        self.requests = []

    def chat(self, request):
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self._script) - 1)
        item = self._script[index]
        if callable(item):
            item = item(request)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, ChatResult):
            return item
        return ChatResult(text=item, usage=Usage(prompt_tokens=100, completion_tokens=40, cost_microusd=self._cost),
                          model=request.model, finish_reason='stop')
