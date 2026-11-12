"""Credential-configurable Fans API mapping; no default network or auto-activation.

The injected HTTP callable must enforce the supplied no-redirect flag and timeout.
Public contract references:
https://docs.onlyfansapi.com/api-reference/account/list-accounts
https://docs.onlyfansapi.com/api-reference/chat-messages/list-chat-messages
https://docs.onlyfansapi.com/api-reference/chat-messages/send-message
https://docs.onlyfansapi.com/api-reference/transactions/list-transactions
https://docs.onlyfansapi.com/api-reference/chargebacks/list-chargebacks
https://docs.onlyfansapi.com/introduction/essentials/idempotency
https://docs.onlyfansapi.com/webhooks/available-events
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from decimal import Decimal
import json
import re
from urllib.parse import urlencode, urlsplit, parse_qsl
from .history import ALLOWED_BASES, parse_history_page, validate_history_next_page
from .intake import _nonfinite, _unique_object
from .send_outcome import classify_send_error
from .send_evidence import body_shape, error_class, provider_evidence
from .webhooks import verify_signature
from conversation_engine.recovery import Outcome

BASE = ALLOWED_BASES[0]
_ACCOUNT = re.compile(r'acct_[A-Za-z0-9_-]{1,240}\Z')
_NUMERIC = re.compile(r'[1-9][0-9]{0,18}\Z')
_KEY = re.compile(r'[\x21-\x7e]{1,255}\Z')
_MAX_BODY = 1_000_000


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    headers: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Result:
    state: str
    data: object = None
    next_page: str | None = None
    trace_id: str | None = None
    http_status: int | None = None
    original_trace_id: str | None = None
    replayed: bool = False
    # Bounded, sanitized failure metadata; never a body, header dump or token.
    failure: str | None = None       # transient | auth | schema
    cause: str | None = None         # fixed code, e.g. provider_rate_limited
    retry_after: int | None = None   # seconds, clamped 0..RETRY_AFTER_MAX
    # Bounded, sanitized account of a send that was not accepted (see send_evidence); never compared or hashed.
    evidence: dict | None = field(default=None, compare=False)


RETRY_AFTER_MAX = 3600


def parse_retry_after(value, now=None):
    """Seconds or HTTP-date to whole seconds clamped to 0..3600; None when absent or malformed."""
    if type(value) is not str:
        return None
    text = value.strip()
    if not text or len(text) > 64:
        return None
    if text.isascii() and (text.isdecimal() or (text[0] == '-' and text[1:].isdecimal())):
        return max(0, min(RETRY_AFTER_MAX, int(text)))
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    delta = (moment - now).total_seconds()
    return max(0, min(RETRY_AFTER_MAX, int(delta) + (1 if delta % 1 else 0)))


def _id(value):
    if type(value) is int and 0 < value < 2**63:
        return str(value)
    if type(value) is str and _NUMERIC.fullmatch(value) and int(value) < 2**63:
        return value
    raise ValueError('invalid numeric provider identity')


def _date(value):
    if type(value) is not str or not re.fullmatch(r'20[0-9]{2}-[01][0-9]-[0-3][0-9] [0-2][0-9]:[0-5][0-9]:[0-5][0-9]', value):
        raise ValueError('invalid provider start date')
    return value


def _media_id(value):
    if type(value) is str and re.fullmatch(r'ofapi_media_[A-Za-z0-9_-]{1,200}', value):
        return value
    return _id(value)


def _object(raw):
    if type(raw) is not bytes or len(raw) > _MAX_BODY:
        raise ValueError('invalid response body')
    return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                      parse_float=Decimal, parse_constant=_nonfinite)


def _trace(headers, name='x-ofapi-trace-id'):
    if type(headers) is not dict:
        return None
    values = [v for k, v in headers.items() if type(k) is str and k.lower() == name]
    if len(values) != 1 or type(values[0]) is not str or not re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', values[0]):
        return None
    return values[0]


class FansApiClient:
    """Explicitly constructed, account/epoch-bound protocol boundary.

    No environment variable enables this object and no default HTTP callable is
    supplied. Integrators must obtain the separately approved egress authority.
    """
    def __init__(self, *, account, creator_id, epoch, token, http, timeout, enabled=False, base=BASE, request_guard=None):
        if (request_guard is not None and not callable(request_guard)):
            raise ValueError('invalid provider request guard')
        if (enabled is not True or type(account) is not str or not _ACCOUNT.fullmatch(account)
                or type(epoch) is not int or epoch < 1 or type(token) is not str or not token.strip() or len(token) > 4096
                or token != token.strip() or any(ord(c) < 33 or ord(c) > 126 for c in token) or not callable(http)
                or type(timeout) not in (int, float) or not 0 < timeout <= 30
                or base not in ALLOWED_BASES):
            raise ValueError('explicit protocol configuration required')
        self.account, self.creator_id, self.epoch = account, _id(creator_id), epoch
        self._token, self._http, self._timeout = token, http, timeout
        self._request_guard = request_guard
        self._identity_verified = False
        self.base = base
        self.last_replayed = False
        self.last_exchange = None    # what the most recent request looked like on the wire (no body, no credentials)
        self.credits_balance = None
        self.last_retry_after = None
        self.last_failure = None

    def _request(self, method, url, body=None, key=None):
        # The durable reservation happens before any provider I/O. A refusal
        # is not swallowed as an ambiguous network error.
        if self._request_guard is not None:
            from .https_transport import is_mock_wire
            self._request_guard(method, url, is_mock_wire(self._http))
        headers = {'Authorization': 'Bearer ' + self._token, 'Accept': 'application/json'}
        if body is not None: headers['Content-Type'] = 'application/json'
        if key is not None: headers['Idempotency-Key'] = key
        self.last_retry_after = None
        exchange = self.last_exchange = {'transport_error': None, 'body_kind': 'none', 'body_bytes': 0}
        from onlyfans_runtime.work_progress import started
        started("provider",self._timeout)
        try:
            response = self._http(method=method, url=url, headers=headers, body=body,
                                  timeout=self._timeout, follow_redirects=False)
        except Exception as exc:
            exchange['transport_error'] = error_class(exc)    # the class only: messages may name hosts or carry secrets
            return None, None, None, None
        if type(response) is not Response or type(response.status) is not int:
            exchange['transport_error'] = 'InvalidResponse'
            return None, None, None, None
        headers = response.headers if type(response.headers) is dict else {}
        lowered = {k.lower(): v for k, v in headers.items() if type(k) is str}
        self.last_replayed = str(lowered.get('idempotent-replayed', '')).strip().lower() == 'true'
        self.last_retry_after = parse_retry_after(lowered.get('retry-after'))
        balance = lowered.get('x-ofapi-credits-balance')
        self.credits_balance = int(balance) if type(balance) is str and balance.isdecimal() else self.credits_balance
        try:
            parsed = _object(response.body)
            parse_ok = True
        except (ValueError, TypeError, UnicodeError, RecursionError):
            parsed, parse_ok = None, False
        exchange['body_kind'], exchange['body_bytes'] = body_shape(response.body, parsed, parse_ok)
        return response.status, parsed, _trace(response.headers), _trace(response.headers, 'x-ofapi-idempotency-original-trace-id')

    def _fail(self, state, status, trace, original_trace, *, failure=None, cause=None):
        """Classify one failed read. Transient: no response, 408/425/429/5xx. Auth: 401/403 and
        identity or account mismatch. Schema: 200 with an invalid shape or any other status."""
        if failure is None:
            if status is None:
                failure, cause = 'transient', 'provider_unreachable'
            elif status == 429:
                failure, cause = 'transient', 'provider_rate_limited'
            elif status in (408, 425) or 500 <= status <= 599:
                failure, cause = 'transient', 'provider_unavailable'
            elif status == 401:
                failure, cause = 'auth', 'provider_auth_rejected'
            elif status == 403:
                failure, cause = 'auth', 'provider_forbidden'
            else:
                failure, cause = 'schema', 'provider_response_invalid'
        retry = self.last_retry_after if failure == 'transient' else None
        result = Result(state, trace_id=trace, http_status=status, original_trace_id=original_trace,
                        failure=failure, cause=cause, retry_after=retry)
        self.last_failure = result
        return result

    def identity(self):
        self._identity_verified = False
        self.last_failure = None
        status, body, trace, original_trace = self._request('GET', self.base + '/api/accounts')
        if status != 200 or type(body) is not list or len(body) > 1000:
            return self._fail('unavailable', status, trace, original_trace)
        matches = [row for row in body if type(row) is dict and row.get('id') == self.account]
        if len(matches) != 1:
            return self._fail('unavailable', status, trace, original_trace, failure='auth', cause='account_not_found')
        try:
            valid = _id(matches[0].get('onlyfans_id')) == self.creator_id
        except ValueError:
            valid = False
        if not valid:
            return self._fail('identity_mismatch', status, trace, original_trace, failure='auth', cause='identity_mismatch')
        if matches[0].get('is_authenticated') is not True:
            return self._fail('identity_mismatch', status, trace, original_trace, failure='auth', cause='account_not_authenticated')
        self._identity_verified = True
        return Result('verified', {'account':self.account, 'creator_id':self.creator_id, 'epoch':self.epoch}, trace_id=trace, http_status=status, original_trace_id=original_trace)

    def history(self, chat_id, *, limit=50, next_page=None, lenient_rows=False):
        """One chat-history page. A failure is classified (transient with Retry-After, auth or schema) exactly like the vault.

        ``lenient_rows`` leaves per-row validation to the caller so one malformed row can be quarantined instead of
        failing the page; the page shape, bound and continuation are still validated."""
        if not self._identity_verified: raise ValueError('verified provider identity required')
        chat = _id(chat_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid page bound')
        url = (validate_history_next_page(next_page, connection_id=self.account, chat_id=chat, base=self.base)
               if next_page is not None else self.base + f'/api/{self.account}/chats/{chat}/messages?' + urlencode({'limit':limit, 'order':'desc'}))
        if next_page is not None:
            pairs = parse_qsl(urlsplit(url).query, keep_blank_values=True, max_num_fields=8)
            allowed = {'limit','filter','first_id','last_id','id','order','skip_users'}
            if len({key for key, _ in pairs}) != len(pairs) or any(key not in allowed for key, _ in pairs):
                raise ValueError('unsupported continuation parameters')
            if any(key == 'limit' and (not value.isdecimal() or not 1 <= int(value) <= 100) for key, value in pairs):
                raise ValueError('invalid continuation limit')
            if any(key in {'first_id','last_id','id'} and (not _NUMERIC.fullmatch(value) or int(value) >= 2**63) for key, value in pairs):
                raise ValueError('invalid continuation cursor')
            if any(key == 'order' and value not in {'asc','desc'} or key == 'filter' and value != 'pinned'
                   or key == 'skip_users' and value not in {'all','none'} for key, value in pairs):
                raise ValueError('invalid continuation parameter')
        status, body, trace, original_trace = self._request('GET', url)
        if status != 200:
            return self._fail('unavailable', status, trace, original_trace)
        try:
            page = parse_history_page(body, connection_id=self.account, chat_id=chat, base=self.base)
            if len(page.messages) > limit or (not lenient_rows and any(type(item) is not dict for item in page.messages)):
                raise ValueError('unbounded or invalid page')
            if not lenient_rows:
                for item in page.messages:
                    _id(item.get('id'))
            if page.next_page == url:
                raise ValueError('non-advancing pagination')
        except (ValueError, TypeError):
            return self._fail('unavailable', status, trace, original_trace, failure='schema', cause='provider_response_invalid')
        return Result('observed', page.messages, page.next_page, trace, status, original_trace)

    def send(self, chat_id, *, key, text, media=(), previews=(), price_cents=0, currency='USD', block_banned_words=None):
        if not self._identity_verified: raise ValueError('verified provider identity required')
        chat = _id(chat_id)
        if (type(key) is not str or not _KEY.fullmatch(key) or type(text) is not str
                or len(text.encode()) > 20000 or type(media) not in (list, tuple) or len(media) > 100
                or type(previews) not in (list, tuple) or len(previews) > len(media)
                or type(price_cents) is not int or (price_cents != 0 and not 300 <= price_cents <= 20000)
                or currency != 'USD' or (not text.strip() and not media) or (price_cents and not media)
                or block_banned_words not in (None, 'strict_ban')):
            raise ValueError('invalid immutable send')
        files = [_media_id(value) for value in media]
        preview_files = [_media_id(value) for value in previews]
        if len(set(files)) != len(files) or len(set(preview_files)) != len(preview_files) or any(v not in files for v in preview_files):
            raise ValueError('invalid media/preview identity')
        fields = ['"text":' + json.dumps(text, ensure_ascii=False)]
        if block_banned_words is not None:
            fields.append('"blockBannedWords":' + json.dumps(block_banned_words))
        if files:
            fields += ['"mediaFiles":' + json.dumps(files, separators=(',', ':')),
                       '"previews":' + json.dumps(preview_files, separators=(',', ':'))]
        if price_cents:
            fields.append('"price":%d.%02d' % divmod(price_cents, 100))
        payload = ('{' + ','.join(fields) + '}').encode('utf-8')
        status, body, trace, original_trace = self._request('POST', self.base + f'/api/{self.account}/chats/{chat}/messages', payload, key)
        if status == 200 and type(body) is dict and type(body.get('data')) is dict:
            try:
                message_id = _id(body['data'].get('id'))
                created = body['data'].get('createdAt')
                if type(created) is not str or not created.strip():
                    raise ValueError('timestamp absent')
                stamp = datetime.fromisoformat(created.replace('Z', '+00:00'))
                if stamp.tzinfo is None or stamp.utcoffset() is None:
                    raise ValueError('timestamp timezone absent')
                return Result('accepted', {'message_id':message_id, 'created_at':created}, trace_id=trace, http_status=status,
                              original_trace_id=original_trace, replayed=self.last_replayed)
            except ValueError:
                pass
        outcome = classify_send_error(status, body)
        if (outcome is Outcome.UNKNOWN and status in (400, 422) and type(body) is dict
                and body.get('error') != 'IDEMPOTENCY_KEY_MISMATCH' and body.get('request_outcome') == 'not_sent'):
            # A refusal the provider itself marks as not sent. An HTTP 4xx alone is NOT proof of non-delivery
            # (docs.onlyfans-api idempotency guide): without request_outcome=not_sent it stays unknown.
            return Result('rejected', trace_id=trace, http_status=status, original_trace_id=original_trace,
                          evidence=self._send_evidence('rejected', status, body, trace, original_trace, text))
        state = ('not_sent' if outcome is Outcome.NOT_SENT else 'key_mismatch' if outcome is Outcome.KEY_MISMATCH
                 else 'conflict' if outcome is Outcome.CONFLICT else 'unknown')
        # Pacing and delivery certainty are independent: a 429 asks for a cooldown (cause + Retry-After) while the
        # send itself stays whatever it was above (unknown). Retry-After never authorizes a resend.
        throttled = status == 429
        return Result(state, trace_id=trace, http_status=status, original_trace_id=original_trace,
                      cause='provider_rate_limited' if throttled else None,
                      retry_after=self.last_retry_after if throttled else None,
                      evidence=self._send_evidence(state, status, body, trace, original_trace, text))

    def _send_evidence(self, state, status, body, trace, original_trace, text):
        # Credentials and the outgoing text are passed only so they can be scrubbed out of any provider message.
        return provider_evidence(outcome=state, status=status, body=body, exchange=self.last_exchange,
                                 trace_id=trace, original_trace_id=original_trace, replayed=self.last_replayed,
                                 forbidden=(self._token, text))

    def vault(self, *, limit=100, offset=0):
        """One page of GET media/vault. Reading the vault sends nothing."""
        if not self._identity_verified: raise ValueError('verified provider identity required')
        if type(limit) is not int or not 10 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 10_000_000:
            raise ValueError('invalid vault page')
        status, body, trace, original_trace = self._request('GET', self.base + f'/api/{self.account}/media/vault?' + urlencode({'limit':limit, 'offset':offset, 'field':'recent', 'sort':'desc'}))
        data = body.get('data') if type(body) is dict else None
        if (status != 200 or type(data) is not dict or type(data.get('list')) is not list
                or type(data.get('hasMore')) is not bool or len(data['list']) > limit
                or any(type(row) is not dict for row in data['list'])):
            return self._fail('unavailable', status, trace, original_trace)
        return Result('observed', tuple(data['list']), 'more' if data['hasMore'] else None, trace, status, original_trace)

    def vault_lists(self):
        if not self._identity_verified: raise ValueError('verified provider identity required')
        status, body, trace, original_trace = self._request('GET', self.base + f'/api/{self.account}/media/vault/lists')
        data = body.get('data') if type(body) is dict else None
        if status != 200 or type(data) is not dict or type(data.get('list')) is not list or any(type(row) is not dict for row in data['list']):
            return self._fail('unavailable', status, trace, original_trace)
        return Result('observed', tuple(data['list']), None, trace, status, original_trace)

    def transactions(self, *, limit=10, marker=None, start_date=None, lenient_rows=False):
        if not self._identity_verified: raise ValueError('verified provider identity required')
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid transaction page bound')
        from transaction_contract import transaction_marker
        params = {'limit':limit}
        if start_date is not None:
            params['startDate'] = _date(start_date)
        if marker is not None:
            params['marker'] = transaction_marker(marker)
        status, body, trace, original_trace = self._request('GET', self.base + f'/api/{self.account}/transactions?' + urlencode(params))
        if status != 200 or type(body) is not dict or type(body.get('data')) is not dict:
            return self._fail('unavailable', status, trace, original_trace)
        data = body['data']
        if type(data.get('list')) is not list or len(data['list']) > limit or type(data.get('hasMore')) is not bool:
            return self._fail('unavailable', status, trace, original_trace, failure='schema', cause='provider_response_invalid')
        if not lenient_rows and any(type(row) is not dict or not ((type(row.get('id')) is str and re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', row['id']))
                or type(row.get('id')) is int and 0 < row['id'] < 2**63) for row in data['list']):
            return self._fail('unavailable', status, trace, original_trace, failure='schema', cause='provider_response_invalid')
        marker_out = data.get('nextMarker')
        if data['hasMore']:
            try:
                marker_out = transaction_marker(marker_out)
                if marker is not None and marker_out == transaction_marker(marker):
                    raise ValueError('non-advancing marker')
            except (ValueError, UnicodeError):
                return self._fail('unavailable', status, trace, original_trace, failure='schema', cause='provider_response_invalid')
        else: marker_out = None
        # Observations are not proof of item ownership or absence.
        return Result('observed', tuple(data['list']), marker_out, trace, status, original_trace)

    def chargebacks(self, *, limit=10, offset=0, start_date=None):
        if not self._identity_verified: raise ValueError('verified provider identity required')
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 1_000_000:
            raise ValueError('invalid chargeback page')
        status, body, trace, original_trace = self._request('GET', self.base + f'/api/{self.account}/chargebacks?' + urlencode({'limit':limit, 'offset':offset, **({'start_date':_date(start_date)} if start_date is not None else {})}))
        # Provider response layout/evidence semantics still need qualification.
        if status != 200 or body is None:
            return self._fail('unavailable', status, trace, original_trace)
        return Result('unverified_observation', body, trace_id=trace, http_status=status, original_trace_id=original_trace)

    def webhook(self, raw, *, signature, secret, event_key):
        if (not verify_signature(raw, signature, secret) or type(event_key) is not str
                or not 1 <= len(event_key) <= 255 or event_key != event_key.strip()
                or any(ord(c) < 33 or ord(c) > 126 for c in event_key)):
            return Result('unavailable')
        try: body = _object(raw)
        except (ValueError, TypeError, UnicodeError, RecursionError): return Result('unavailable')
        if type(body) is not dict or body.get('account_id') != self.account or type(body.get('payload')) is not dict:
            return Result('scope_mismatch')
        event = body.get('event')
        if event not in {'messages.received','messages.sent','messages.ppv.unlocked','transactions.new',
                         'accounts.connected','accounts.reconnected','accounts.disconnected','accounts.session_expired'}:
            return Result('unsupported')
        return Result('observation', {'event':event, 'payload':body['payload'], 'event_key':event_key,
                                      'account':self.account, 'epoch':self.epoch})
