"""Bounded, sanitized evidence about one provider send exchange. Pure functions: no I/O, no database, no logging.

Why this exists: a send that is neither accepted nor proven not-sent used to leave nothing behind except the reason
``invalid_receipt`` (every "no usable receipt" result takes that route), so an operator could not tell a timeout from a
conflict from a malformed 200, and the provider response was thrown away.

Kept:   HTTP status, provider error code/type, ``request_outcome``, whether a provider message id / timestamp was present
        (and the numeric id itself when it is valid), trace ids, a short redacted provider message, the CLASS NAME of a
        transport error, which response field was unusable, counts.
Never:  credentials, the outgoing text, fan text, raw response bodies, exception messages, free-form provider values.

Every function is total: hostile input (huge, mistyped, binary, recursive) yields a small safe value, never an exception.
"""
from datetime import datetime
import re

MESSAGE_MAX = 300
CODE_MAX = 64
_CODE = re.compile(r'[A-Za-z0-9_.:-]{1,64}')
_OUTCOMES = frozenset({'not_sent', 'unknown', 'sent', 'executed'})
_URL = re.compile(r'[A-Za-z][A-Za-z0-9+.\-]*://\S+')
_EMAIL = re.compile(r'\S+@\S+\.\S+')
_SECRET_WORD = re.compile(r'(?i)\b(?:bearer|basic|authorization|api[_ -]?key|token|secret|password|passwd|session(?:id)?|cookie)\b\s*[:=]?\s*\S*')
_LONG = re.compile(r'[A-Za-z0-9_\-+/=.]{24,}')
_NUMBER = re.compile(r'[0-9]{6,}')
_WORK_BOUND = 2000     # characters of a provider message ever examined


def error_class(exc):
    """The class name of a transport error (never its message, which may carry hosts or credentials)."""
    name = getattr(type(exc), '__name__', 'Error')
    return name if re.fullmatch(r'[A-Za-z0-9_]{1,64}', name or '') else 'Error'


def clean_code(value):
    """A provider code/type kept only when it is a short token; an odd string is recorded as '(invalid)'."""
    if type(value) is not str:
        return None
    return value if _CODE.fullmatch(value) else '(invalid)'


def _printable(text):
    return ''.join(ch if ' ' <= ch <= '~' else ' ' for ch in text)


def _norm(text):
    return ' '.join(text.lower().split())


def _echoes(message, sources, size=12):
    grams = {message[i:i + size] for i in range(len(message) - size + 1)}
    if not grams:
        return False
    for source in sources:
        source = _norm(source)[:20000]
        if len(source) >= size and any(source[i:i + size] in grams for i in range(len(source) - size + 1)):
            return True
    return False


def redact_message(value, forbidden=()):
    """A provider message reduced to at most 300 printable ASCII characters with secrets, URLs, e-mail addresses,
    long tokens, long numbers and any copy of ``forbidden`` strings (credentials, the outgoing text) removed.
    A message that quotes the outgoing text at length is dropped entirely."""
    if type(value) is not str:
        return None
    text = _printable(value[:_WORK_BOUND])
    secrets = [s for s in forbidden if type(s) is str and len(s.strip()) >= 3]
    for secret in secrets:
        for variant in {secret.strip(), _printable(secret).strip()}:
            if len(variant) >= 3:
                text = re.sub(re.escape(variant), '[redacted]', text, flags=re.I)
    for pattern, mark in ((_URL, '[url]'), (_EMAIL, '[email]'), (_SECRET_WORD, '[secret]'), (_LONG, '[token]'),
                          (_NUMBER, '[number]')):
        text = pattern.sub(mark, text)
    text = ' '.join(text.split())
    if not text or _echoes(_norm(text), secrets):
        return None
    return text[:MESSAGE_MAX]


def _numeric_id(value):
    if type(value) is int and not isinstance(value, bool) and 0 < value < 2**63:
        return str(value)
    if type(value) is str and re.fullmatch(r'[1-9][0-9]{0,18}', value) and int(value) < 2**63:
        return value
    return None


def _created_state(value):
    if value is None or value == '':
        return 'absent'
    if type(value) is not str:
        return 'invalid'
    try:
        stamp = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return 'invalid'
    return 'ok' if stamp.tzinfo is not None and stamp.utcoffset() is not None else 'no_timezone'


def body_shape(raw, parsed, parse_ok):
    """('none'|'empty'|'json_object'|'json_array'|'json_scalar'|'unparseable', byte count) for a response body."""
    size = len(raw) if type(raw) is bytes else 0
    if not parse_ok:
        return ('empty' if size == 0 else 'unparseable'), size
    return ('json_object' if type(parsed) is dict else 'json_array' if type(parsed) is list else 'json_scalar'), size


def provider_evidence(*, outcome, status, body, exchange, trace_id, original_trace_id, replayed, forbidden=(), posts=None):
    """The ``provider`` section of the attempt evidence for one non-accepted send result. Never raises."""
    try:
        return _provider_evidence(outcome, status, body, exchange, trace_id, original_trace_id, replayed, forbidden, posts)
    except Exception:    # noqa: BLE001 - evidence must never break a send
        return {'outcome': outcome if type(outcome) is str and len(outcome) <= 16 else 'unknown',
                'http_status': status if type(status) is int and 100 <= status <= 599 else None}


def _provider_evidence(outcome, status, body, exchange, trace_id, original_trace_id, replayed, forbidden, posts):
    exchange = exchange if type(exchange) is dict else {}
    out = {
        'outcome': outcome if type(outcome) is str and re.fullmatch(r'[a-z_]{1,16}', outcome) else 'unknown',
        'http_status': status if type(status) is int and 100 <= status <= 599 else None,
        'transport_error': exchange.get('transport_error') if type(exchange.get('transport_error')) is str else None,
        'body_kind': exchange.get('body_kind') if type(exchange.get('body_kind')) is str else 'none',
        'body_bytes': exchange.get('body_bytes') if type(exchange.get('body_bytes')) is int else 0,
        'trace_id': trace_id if type(trace_id) is str and len(trace_id) <= 128 else None,
        'original_trace_id': original_trace_id if type(original_trace_id) is str and len(original_trace_id) <= 128 else None,
        'replayed': replayed is True,
    }
    if type(posts) is int and 0 <= posts <= 10:
        out['posts'] = posts
    d = body if type(body) is dict else {}
    error = d.get('error')
    code, kind, message = error, None, None
    if type(error) is dict:
        code, kind, message = error.get('code'), error.get('type'), error.get('message')
    if kind is None:
        kind = d.get('error_type', d.get('type'))
    if message is None:
        message = next((d[k] for k in ('message', 'error_description', 'detail') if type(d.get(k)) is str), None)
    request_outcome = d.get('request_outcome')
    out['error_code'] = clean_code(code)
    out['error_type'] = clean_code(kind)
    out['request_outcome'] = (request_outcome if type(request_outcome) is str and request_outcome in _OUTCOMES
                              else None if request_outcome is None else '(other)')
    out['message'] = redact_message(message, forbidden)
    data = d.get('data')
    data = data if type(data) is dict else None
    ident = _numeric_id(data.get('id')) if data is not None else None
    created = _created_state(data.get('createdAt')) if data is not None else 'absent'
    out['message_id_present'] = data is not None and data.get('id') is not None
    out['message_id'] = ident
    out['created_at_state'] = created
    if created == 'ok':
        out['provider_created_at'] = data['createdAt'].strip()[:40]
    # For a 200 that was not accepted: the response field that made it unusable.
    out['failed_field'] = ((None if status != 200 else 'data' if data is None else 'data.id' if ident is None
                            else 'data.createdAt' if created != 'ok' else None))
    return out


_PROVIDER_FIELDS = {   # key -> (type, max length of a str)
    'outcome': (str, 16), 'http_status': (int, 0), 'transport_error': (str, 64), 'body_kind': (str, 16),
    'body_bytes': (int, 0), 'trace_id': (str, 128), 'original_trace_id': (str, 128), 'replayed': (bool, 0),
    'posts': (int, 0), 'error_code': (str, 64), 'error_type': (str, 64), 'request_outcome': (str, 16),
    'message': (str, MESSAGE_MAX), 'message_id_present': (bool, 0), 'message_id': (str, 19), 'created_at_state': (str, 16),
    'provider_created_at': (str, 40), 'failed_field': (str, 16), 'retry_after': (int, 0),
}


def clamp_provider_section(value):
    """Defence in depth at the storage boundary: only known keys of the right type and size, whatever the caller built."""
    out = {}
    if type(value) is not dict:
        return out
    for key, (kind, limit) in _PROVIDER_FIELDS.items():
        item = value.get(key)
        if type(item) is not kind or (kind is str and len(item) > limit):
            continue
        out[key] = item
    return out
