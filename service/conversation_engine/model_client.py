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
    model: str = ''
    finish_reason: str | None = None


# ---- errors -------------------------------------------------------------------
class ModelCallError(Exception):
    """Base class. Never carries a secret; `error_class` is stored in the call record."""
    error_class = 'model_error'


class ModelNotSent(ModelCallError):
    """Proven to have failed before any request bytes were sent. No charge is possible."""
    error_class = 'not_sent'


class ModelHTTPStatus(ModelCallError):
    """The provider answered with a non-success status. Providers do not charge for these."""
    def __init__(self, status):
        super().__init__(f'provider returned HTTP {status}')
        self.status = status
        self.error_class = f'http_{status}'


class ModelOutcomeUnknown(ModelCallError):
    """The request may have reached the provider (timeout, reset after send). Cost is unknown."""
    error_class = 'unknown_outcome'


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
