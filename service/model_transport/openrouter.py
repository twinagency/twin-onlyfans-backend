"""OpenRouter-compatible chat client (httpx). The only module on the engine's model path that touches the network.

Default OFF: nothing constructs this client unless ModelConfig.enabled is true and the entry point calls
`model_transport.build_model_runtime`. The API key is never stored in the repository or in config: it is read
from a file the process owns with mode 0600, or from the environment variable whose NAME is configured. The key is
never logged, never included in an error, and is redacted in repr().

Failure classification (it decides how the ledger counts the call):
  ConnectError/ConnectTimeout/PoolTimeout           -> ModelNotSent        (proved unsent: released, cost 0)
  non-200 response                                  -> ModelHTTPStatus     (provider refused: released, cost 0)
  read/write timeout, reset, protocol error, junk, or an error inside HTTP 200
                                                    -> ModelOutcomeUnknown (may have been charged: counted as spent)
"""
import json
from hashlib import sha256
from decimal import Decimal
import os
import re
import stat
from urllib.parse import urlsplit, quote

import httpx

from conversation_engine.model_client import (ChatResult, ModelHTTPStatus, ModelIncompleteResponse, ModelNotSent,
                                              ModelOutcomeUnknown, Usage)

_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol,
             httpx.InvalidURL, httpx.LocalProtocolError, httpx.ProxyError)


class KeyUnavailable(Exception):
    pass


# ---- non-200 evidence ----------------------------------------------------------------
# Bounded and sanitized: never the key, the prompt or a raw body. Only short redacted scalar fields survive;
# free-text messages are kept as a presence marker only.
_MAX_ERROR_BODY = 65_536
_SAFE = re.compile(r'[^A-Za-z0-9 ._:/@()\[\],;=+\-\'"?!%#]')
_SECRET = re.compile(r'(sk-[A-Za-z0-9_-]{6,}|Bearer\s+\S+|[A-Za-z0-9_-]{32,})')


def _clean(value, limit):
    # Strip first, then redact: stripping after redaction could rejoin a split token. Every field is redacted.
    if type(value) is int and not isinstance(value, bool):
        return value if -10**9 < value < 10**9 else None
    if type(value) is not str or not value:
        return None
    return _SECRET.sub('[redacted]', _SAFE.sub('', value))[:limit] or None


def _message_marker(value):
    # Free-text provider messages can echo the prompt (fan text); no pattern can tell, so never store the text.
    return '[redacted]' if type(value) is str and value.strip() else None


def _retry_after(value, *, now=None):
    """Retry-After as whole seconds (delta or HTTP-date), clamped to 0..3600; None when absent or unreadable."""
    if type(value) is not str or not value.strip() or len(value) > 64:
        return None
    value = value.strip()
    if value.isdigit():
        return min(int(value), 3600)
    from email.utils import parsedate_to_datetime
    from datetime import datetime, timezone
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    seconds = (when - (now or datetime.now(timezone.utc))).total_seconds()
    return max(0, min(int(seconds + 0.999), 3600))


def _failure_detail(response):
    headers = response.headers
    detail = {'status': response.status_code}
    for name, key in (('x-request-id', 'requestId'), ('retry-after', 'retryAfter'), ('x-ratelimit-limit', 'rateLimitLimit'),
                      ('x-ratelimit-remaining', 'rateLimitRemaining'), ('x-ratelimit-reset', 'rateLimitReset')):
        value = _clean(headers.get(name), 64)
        if value is not None:
            detail[key] = value
    content = response.content if len(response.content) <= _MAX_ERROR_BODY else b''
    try:
        body = json.loads(content) if content else None
    except (ValueError, UnicodeError, RecursionError):
        body = None
    error = body.get('error') if type(body) is dict else None
    if type(error) is dict:
        for key, value in (('Code', _clean(error.get('code'), 32)), ('Type', _clean(error.get('type'), 64)),
                           ('Message', _message_marker(error.get('message')))):
            if value is not None:
                detail['error' + key] = value
        meta = error.get('metadata') if type(error.get('metadata')) is dict else {}
        provider = _clean(meta.get('provider_name'), 64)
        if provider is not None:
            detail['providerName'] = provider
    elif type(error) is str:
        value = _message_marker(error)
        if value is not None:
            detail['errorMessage'] = value
    return detail


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
                 title='twin-engine', upstream_provider='', price_caps=None):
        parts = urlsplit(base_url)
        if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
            raise ValueError('the model base URL must be https without credentials')
        self._url = base_url.rstrip('/') + '/chat/completions'
        self._key = load_key(key_file=key_file, key_env=key_env, environ=environ)
        self._transport = transport  # httpx.MockTransport in tests; None = the real network
        self._title = title
        if (type(upstream_provider) is not str or upstream_provider
                and not re.fullmatch(r'[a-z0-9][a-z0-9._:/-]{0,127}', upstream_provider)):
            raise ValueError('invalid upstream provider')
        if upstream_provider and (type(price_caps) is not dict or not price_caps or any(
                type(model) is not str or not model or type(prices) is not tuple or len(prices) != 2
                or any(type(price) is not int or not 0 < price <= 10**12 for price in prices)
                for model, prices in price_caps.items())):
            raise ValueError('single-provider routing requires bounded token prices')
        self._upstream_provider = upstream_provider
        self._price_caps = dict(price_caps or {})

    def fixture_transport(self):
        return type(self._transport) is httpx.MockTransport

    def __repr__(self):
        return f'OpenRouterClient(url={self._url!r}, key=<redacted>)'

    def endpoint_fingerprint(self):
        return sha256(self._url.encode()).hexdigest()

    def _generation_reference(self, data):
        ident = data.get('id')
        if type(ident) is str and re.fullmatch(r'[A-Za-z0-9._:-]{1,255}', ident):
            return ident, self.endpoint_fingerprint()
        return '', ''

    def lookup_canonical_model(self, requested_model, *, timeout_seconds=30):
        """Authenticated provider mapping read; no generation or inferred alias."""
        from onlyfans_runtime.store import Refused
        if (type(requested_model) is not str or len(requested_model)>128 or
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._:-]*",requested_model)):
            raise Refused("invalid model mapping identity")
        author,slug=requested_model.split("/",1)
        url=self._url.removesuffix("/chat/completions")+"/model/"+quote(author,safe="")+"/"+quote(slug,safe=":")
        try:
            with httpx.Client(transport=self._transport,timeout=httpx.Timeout(timeout_seconds),
                    follow_redirects=False,trust_env=False) as client:
                response=client.get(url,headers={"Authorization":"Bearer "+self._key})
        except httpx.HTTPError:
            raise Refused("provider model mapping is unavailable") from None
        if response.status_code!=200 or len(response.content)>1_000_000:
            raise Refused("provider model mapping is unavailable")
        def unique(pairs):
            result={}
            for key,value in pairs:
                if key in result:raise ValueError("duplicate mapping field")
                result[key]=value
            return result
        try:
            raw=json.loads(response.content,object_pairs_hook=unique)
            data=raw["data"]
            canonical=data["canonical_slug"]
            if (type(data) is not dict or data.get("id")!=requested_model or
                    type(canonical) is not str or len(canonical)>128 or
                    not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}",canonical)):
                raise ValueError("invalid provider model mapping")
        except (ValueError,TypeError,KeyError,AttributeError,UnicodeError,RecursionError):
            raise Refused("provider model mapping is incomplete or conflicting") from None
        return {"requestedModel":requested_model,"canonicalModel":canonical,
                "endpointSha256":self.endpoint_fingerprint(),
                "mappingReceiptSha256":sha256(response.content).hexdigest()}

    def lookup_generation(self, generation_id, *, timeout_seconds=30):
        """One manual authenticated metadata read; no generation, retry or inferred zero cost."""
        from onlyfans_runtime.store import Refused
        if type(generation_id) is not str or not re.fullmatch(r'[A-Za-z0-9._:-]{1,255}', generation_id):
            raise Refused('provider generation correlation is unavailable')
        try:
            with httpx.Client(transport=self._transport, timeout=httpx.Timeout(timeout_seconds),
                    follow_redirects=False, trust_env=False) as client:
                response = client.get(self._url.removesuffix('/chat/completions') + '/generation',
                    params={'id':generation_id}, headers={'Authorization':'Bearer '+self._key})
        except httpx.HTTPError:
            raise Refused('provider billing evidence is unavailable') from None
        if response.status_code != 200 or len(response.content) > 1_000_000:
            raise Refused('provider billing evidence is unavailable')
        def unique(pairs):
            result = {}
            for key,value in pairs:
                if key in result:raise ValueError('duplicate provider billing field')
                result[key] = value
            return result
        try:
            raw = json.loads(response.content, parse_float=Decimal, object_pairs_hook=unique)
            data = raw['data']
            if type(data) is not dict or data.get('id') != generation_id:
                raise ValueError('generation mismatch')
            value = data.get('total_cost')
            if type(value) not in (int,Decimal):raise ValueError('cost unavailable')
            amount = Decimal(value)
            if (not amount.is_finite() or amount < 0 or amount*1_000_000 > 2**63-1
                    or type(data.get('model')) is not str or not data['model']
                    or data.get('finish_reason') not in ('stop','length','tool_calls','content_filter','error')):
                raise ValueError('incomplete billing evidence')
            # Same nearest-microUSD accounting unit as chat Usage; retain exact USD too.
            cost = int(round(amount*1_000_000))
        except (ValueError,TypeError,KeyError,ArithmeticError,UnicodeError,RecursionError):
            raise Refused('provider billing evidence is incomplete or uncorrelated') from None
        return {'generationId':generation_id,'endpointSha256':self.endpoint_fingerprint(),
            'model':data['model'],'totalCostUsd':str(amount),'actualMicrousd':cost,
            'finishReason':data['finish_reason'],
            'providerName':data.get('provider_name') if type(data.get('provider_name')) is str else None,
            'createdAt':data.get('created_at') if type(data.get('created_at')) is str else None,
            'receiptSha256':sha256(response.content).hexdigest()}

    def chat(self, request):
        body = {'model': request.model, 'messages': list(request.messages), 'temperature': request.temperature,
                'max_tokens': request.max_tokens, 'usage': {'include': True}}
        if request.json_mode:
            body['response_format'] = {'type': 'json_object'}
        if self._upstream_provider:
            prices = self._price_caps.get(request.model)
            if prices is None:
                raise ModelNotSent('model has no configured price bound')
            body['provider'] = {'only': [self._upstream_provider], 'allow_fallbacks': False,
                'require_parameters': True,
                'max_price': {'prompt': prices[0] / 1_000_000, 'completion': prices[1] / 1_000_000}}
        headers = {'Authorization': 'Bearer ' + self._key, 'Content-Type': 'application/json', 'X-Title': self._title,
                   'X-OpenRouter-Metadata': 'enabled'}
        try:
            with httpx.Client(transport=self._transport, timeout=httpx.Timeout(request.timeout_seconds),
                              follow_redirects=False, trust_env=False) as client:
                response = client.post(self._url, content=json.dumps(body).encode('utf-8'), headers=headers)
        except _NOT_SENT:
            raise ModelNotSent('request was not sent') from None
        except httpx.HTTPError:
            raise ModelOutcomeUnknown('the request may have reached the provider') from None
        if response.status_code != 200:
            raise ModelHTTPStatus(response.status_code, detail=_failure_detail(response),
                                  retry_after=_retry_after(response.headers.get('retry-after')))
        try:
            data = response.json()
        except ValueError:
            raise ModelOutcomeUnknown('provider answered with an unreadable body') from None
        if type(data) is not dict:
            raise ModelOutcomeUnknown('provider answered with an unreadable body')
        generation_id, endpoint_sha256 = self._generation_reference(data)
        if data.get('error') is not None:
            raise ModelIncompleteResponse('provider reported an incomplete response',
                generation_id=generation_id, endpoint_sha256=endpoint_sha256)
        usage = data.get('usage') if type(data.get('usage')) is dict else {}
        parsed = Usage(prompt_tokens=_count(usage.get('prompt_tokens')), completion_tokens=_count(usage.get('completion_tokens')),
                       cost_microusd=_microusd(usage.get('cost')))
        choices = data.get('choices')
        choice = choices[0] if type(choices) is list and choices and type(choices[0]) is dict else {}
        if choice.get('error') is not None or choice.get('finish_reason') == 'error':
            raise ModelIncompleteResponse('provider reported an incomplete response',
                generation_id=generation_id, endpoint_sha256=endpoint_sha256)
        message = choice.get('message') if type(choice.get('message')) is dict else {}
        text = message.get('content') if type(message.get('content')) is str else ''
        # `model` is what the provider says answered. A response without one is recorded as such (empty), never
        # silently replaced by the requested id, so the ledger can tell "same model" from "not reported".
        returned = data.get('model') if type(data.get('model')) is str else ''
        upstream = data.get('provider') if type(data.get('provider')) is str else ''
        meta = data.get('openrouter_metadata') if type(data.get('openrouter_metadata')) is dict else {}
        endpoints = meta.get('endpoints') if type(meta.get('endpoints')) is dict else {}
        available = endpoints.get('available') if type(endpoints.get('available')) is list else []
        selected = [row.get('provider') for row in available if type(row) is dict and row.get('selected') is True
                    and type(row.get('provider')) is str and row.get('provider')]
        if len(selected) == 1:
            upstream = selected[0] if not upstream or upstream.casefold() == selected[0].casefold() else ''
        elif selected:
            upstream = ''  # conflicting metadata is not a verified identity
        return ChatResult(text=text, usage=parsed, model=returned[:128], provider=upstream[:64],
                          generation_id=generation_id, endpoint_sha256=endpoint_sha256,
                          finish_reason=choice.get('finish_reason') if type(choice.get('finish_reason')) is str else None)
