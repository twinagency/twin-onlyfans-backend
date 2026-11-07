"""OpenRouter-compatible chat client (httpx). The only module on the engine's model path that touches the network.

Default OFF: nothing constructs this client unless ModelConfig.enabled is true and the entry point calls
`model_transport.build_model_runtime`. The API key is never stored in the repository or in config: it is read
from a file the process owns with mode 0600, or from the environment variable whose NAME is configured. The key is
never logged, never included in an error, and is redacted in repr().

Failure classification (it decides how the ledger counts the call):
  ConnectError/ConnectTimeout/PoolTimeout           -> ModelNotSent        (proved unsent: released, cost 0)
  non-200 response                                  -> ModelHTTPStatus     (provider refused: released, cost 0)
  read/write timeout, reset, protocol error, junk   -> ModelOutcomeUnknown (may have been charged: counted as spent)
"""
import json
import os
import stat
from urllib.parse import urlsplit

import httpx

from conversation_engine.model_client import (ChatResult, ModelHTTPStatus, ModelNotSent, ModelOutcomeUnknown, Usage)

_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol,
             httpx.InvalidURL, httpx.LocalProtocolError, httpx.ProxyError)


class KeyUnavailable(Exception):
    pass


def load_key(*, key_file='', key_env='', environ=None):
    """Return the API key from a 0600 regular file owned by this user, or from the env var named `key_env`."""
    environ = os.environ if environ is None else environ
    if bool(key_file) == bool(key_env):
        raise KeyUnavailable('configure exactly one of a key file or a key env var name')
    if key_env:
        value = environ.get(key_env, '')
    else:
        try:
            info = os.lstat(key_file)
        except OSError:
            raise KeyUnavailable('model key file is unavailable') from None
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid()):
            raise KeyUnavailable('model key file must be a regular file owned by this user with mode 0600')
        with open(key_file, encoding='utf-8') as handle:
            value = handle.read()
    value = value.strip()
    if not value or any(c.isspace() for c in value) or len(value) > 512:
        raise KeyUnavailable('model key is missing or malformed')
    return value


def _microusd(value):
    if type(value) in (int, float) and value >= 0 and value == value and value != float('inf'):
        return int(round(value * 1_000_000))
    return None


def _count(value):
    return value if type(value) is int and value >= 0 else None


class OpenRouterClient:
    provider = 'openrouter'

    def __init__(self, *, base_url='https://openrouter.ai/api/v1', key_file='', key_env='', environ=None, transport=None,
                 title='twin-engine'):
        parts = urlsplit(base_url)
        if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
            raise ValueError('the model base URL must be https without credentials')
        self._url = base_url.rstrip('/') + '/chat/completions'
        self._key = load_key(key_file=key_file, key_env=key_env, environ=environ)
        self._transport = transport  # httpx.MockTransport in tests; None = the real network
        self._title = title

    def __repr__(self):
        return f'OpenRouterClient(url={self._url!r}, key=<redacted>)'

    def chat(self, request):
        body = {'model': request.model, 'messages': list(request.messages), 'temperature': request.temperature,
                'max_tokens': request.max_tokens, 'usage': {'include': True}}
        if request.json_mode:
            body['response_format'] = {'type': 'json_object'}
        headers = {'Authorization': 'Bearer ' + self._key, 'Content-Type': 'application/json', 'X-Title': self._title}
        try:
            with httpx.Client(transport=self._transport, timeout=httpx.Timeout(request.timeout_seconds),
                              follow_redirects=False, trust_env=False) as client:
                response = client.post(self._url, content=json.dumps(body).encode('utf-8'), headers=headers)
        except _NOT_SENT:
            raise ModelNotSent('request was not sent') from None
        except httpx.HTTPError:
            raise ModelOutcomeUnknown('the request may have reached the provider') from None
        if response.status_code != 200:
            raise ModelHTTPStatus(response.status_code)
        try:
            data = response.json()
        except ValueError:
            raise ModelOutcomeUnknown('provider answered with an unreadable body') from None
        if type(data) is not dict:
            raise ModelOutcomeUnknown('provider answered with an unreadable body')
        usage = data.get('usage') if type(data.get('usage')) is dict else {}
        parsed = Usage(prompt_tokens=_count(usage.get('prompt_tokens')), completion_tokens=_count(usage.get('completion_tokens')),
                       cost_microusd=_microusd(usage.get('cost')))
        choices = data.get('choices')
        choice = choices[0] if type(choices) is list and choices and type(choices[0]) is dict else {}
        message = choice.get('message') if type(choice.get('message')) is dict else {}
        text = message.get('content') if type(message.get('content')) is str else ''
        # `model` is what the provider says answered. A response without one is recorded as such (empty), never
        # silently replaced by the requested id, so the ledger can tell "same model" from "not reported".
        returned = data.get('model') if type(data.get('model')) is str else ''
        upstream = data.get('provider') if type(data.get('provider')) is str else ''
        return ChatResult(text=text, usage=parsed, model=returned[:128], provider=upstream[:64],
                          finish_reason=choice.get('finish_reason') if type(choice.get('finish_reason')) is str else None)
