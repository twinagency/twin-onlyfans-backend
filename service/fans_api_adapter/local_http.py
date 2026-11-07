"""Loopback-only HTTP listener and Fans transport for deterministic lab tests.

This deliberately cannot connect to a remote host or use real credentials. It
exercises actual HTTP serialization and server handling around the canonical
inbox and delivery authority; it is not a qualified live-provider adapter.
"""
from collections import deque
from decimal import Decimal
from hashlib import sha256
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import re
import queue
import time
import secrets
import socket
import threading

from onlyfans_runtime.store import Refused
from .intake import _unique_object, _nonfinite
from .synthetic_transport import FansApiSyntheticTransport, SyntheticFansProvider

_ACCOUNT = re.compile(r'acct_[A-Za-z0-9_-]{1,240}\Z')
_CHAT = re.compile(r'[1-9][0-9]{0,18}\Z')
_SEND_PATH = re.compile(r'/api/(acct_[A-Za-z0-9_-]{1,240})/chats/([1-9][0-9]{0,18})/messages\Z')
_MAX_BODY = 1_000_000


def _json_bytes(value):
    def convert(item):
        if type(item) is Decimal and item.is_finite():
            return float(item)
        raise TypeError('unsupported fixture JSON value')
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=convert, allow_nan=False).encode('utf-8')


class LocalFansHttpLab:
    """An ephemeral loopback fixture; all I/O stays inside its network namespace."""
    def __init__(self, provider):
        if type(provider) is not SyntheticFansProvider:
            raise Refused('only a synthetic provider fixture is allowed')
        self.provider = provider
        self._token = secrets.token_urlsafe(32)
        self._worker = self._secret = self._scope = None
        self._jobs = queue.Queue(maxsize=32)
        self._result_condition = threading.Condition()
        self._results = {}
        self._recovered = {}
        self._maintenance_runs = 0
        self._rescan = threading.Event()
        self._stopping = threading.Event()
        self._next_ticket = 0
        self._processor = None
        lab = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return  # never log webhook bodies, tokens, or customer text

            def _reply(self, status, payload):
                encoded = _json_bytes(payload)
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_POST(self):
                try:
                    lengths = self.headers.get_all('Content-Length', [])
                    if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
                        return self._reply(400, {'error': 'invalid_length'})
                    length = int(lengths[0])
                    if length > _MAX_BODY or self.headers.get('Transfer-Encoding'):
                        return self._reply(413, {'error': 'invalid_length'})
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        return self._reply(400, {'error': 'truncated'})
                    if self.path == '/webhooks/fans':
                        signatures = self.headers.get_all('X-Webhook-Signature', [])
                        keys = self.headers.get_all('X-Event-Id', [])
                        if len(signatures) != 1 or len(keys) != 1 or lab._worker is None:
                            return self._reply(401, {'error': 'fixture_authentication_failed'})
                        tenant, account = lab._scope
                        # Acknowledgement follows committed intake, even if the
                        # downstream worker must be retried or reviewed.
                        intake = lab._worker.adapter.accept(raw, signatures[0], lab._secret, keys[0],
                            expected_tenant=tenant, expected_account=account)
                        if intake == 'quarantined':
                            return self._reply(202, {'intake': intake, 'outcome': 'quarantined'})
                        # Queue only after durable authenticated intake. A slow
                        # model/provider cannot delay the HTTP acknowledgement.
                        ticket = lab._enqueue(raw, signatures[0], keys[0])
                        return self._reply(202, {'intake': intake, 'outcome': 'queued' if ticket else 'deferred',
                                                 'receipt': ticket})
                    match = _SEND_PATH.fullmatch(self.path)
                    tokens = self.headers.get_all('X-Synthetic-Token', [])
                    keys = self.headers.get_all('Idempotency-Key', [])
                    if (match is None or len(tokens) != 1 or not hmac.compare_digest(tokens[0], lab._token)
                            or len(keys) != 1 or not re.fullmatch(r'[0-9a-f-]{36}', keys[0])):
                        return self._reply(403, {'error': 'fixture_scope_refused'})
                    body = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object, parse_float=Decimal, parse_constant=_nonfinite)
                    if type(body) is not dict:
                        return self._reply(400, {'error': 'invalid_payload'})
                    request = {'account_id': match.group(1), 'chat_id': match.group(2),
                               'idempotency_key': keys[0], 'body': body}
                    try:
                        status, response = lab.provider.send(request)
                    except TimeoutError:
                        # Simulate an accepted send whose HTTP response is lost.
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                        return
                    return self._reply(status, response)
                except Refused:
                    return self._reply(401, {'error': 'fixture_scope_refused'})
                except (UnicodeError, ValueError, TypeError, OSError):
                    return self._reply(400, {'error': 'invalid_fixture_request'})

        self._server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        self._thread.start()

    @property
    def port(self):
        if self._server.server_address[0] != '127.0.0.1' or not self._thread.is_alive():
            raise Refused('local fixture server is unavailable')
        return self._server.server_address[1]

    def attach(self, worker, *, tenant, account, secret):
        from conversation_engine.worker import SyntheticConversationWorker
        if (type(worker) is not SyntheticConversationWorker or type(worker.transport) is not FansApiLocalHttpTransport
                or worker.transport.lab is not self or worker.transport.tenant_id != tenant
                or worker.transport.account_id != account or type(secret) is not bytes or not secret):
            raise Refused('invalid local webhook listener scope')
        if self._worker is not None:
            raise Refused('local webhook listener is already attached')
        self._worker, self._scope, self._secret = worker, (tenant, account), secret
        from onlyfans_runtime.store import utcnow
        self._initial_upper = utcnow()  # freeze the restart recovery snapshot
        self._processor = threading.Thread(target=self._process, daemon=True)
        self._processor.start()

    def _enqueue(self, raw, signature, event_key):
        with self._result_condition:
            self._next_ticket += 1
            ticket = self._next_ticket
            try:
                self._jobs.put_nowait((ticket, raw, signature, event_key))
            except queue.Full:
                self._rescan.set()  # durable webhook will be scanned after the queue drains
                return None
            return ticket

    def _recovery_page(self, cursor, upper):
        from sqlalchemy import and_, or_, select
        from sqlalchemy.orm import Session
        from db.onlyfans_engine_models import OfFansConnection, OfFansWebhook
        tenant, account = self._scope
        with Session(self._worker.engine.store.engine) as s, s.begin():
            runtime = self._worker.engine.store._runtime(s, tenant, account)
            connection = s.scalar(select(OfFansConnection).where(OfFansConnection.account_id == account,
                OfFansConnection.binding_id == runtime.binding_id, OfFansConnection.status == 'active'))
            if connection is None:
                return []
            query = select(OfFansWebhook.received_at, OfFansWebhook.event_key, OfFansWebhook.raw_body,
                OfFansWebhook.payload_sha256).where(OfFansWebhook.account_id == account,
                OfFansWebhook.epoch == connection.epoch, OfFansWebhook.status == 'accepted',
                OfFansWebhook.received_at <= upper)
            if cursor is not None:
                query = query.where(or_(OfFansWebhook.received_at > cursor[0],
                    and_(OfFansWebhook.received_at == cursor[0], OfFansWebhook.event_key > cursor[1])))
            return list(s.execute(query.order_by(OfFansWebhook.received_at, OfFansWebhook.event_key).limit(32)).all())

    def _process(self):
        # One bounded worker gives live intake priority. Startup and saturation
        # scans use immutable webhook evidence in keyset pages of 32; replays
        # reuse the canonical request and never create a new submission identity.
        from onlyfans_runtime.store import utcnow
        from onlyfans_runtime.text_delivery import TextAuthority
        authority = TextAuthority(self._worker.engine.store)
        backlog, cursor, upper = deque(), None, self._initial_upper
        next_maintenance = 0.0
        while True:
            if self._stopping.is_set():
                return
            if time.monotonic() >= next_maintenance:
                try:
                    tenant, account = self._scope
                    authority.expire_dispatches(tenant, account, limit=32, synthetic_engine_only=True)
                    authority.repair(tenant, account, limit=32, synthetic_engine_only=True)
                except Exception:
                    pass  # durable work remains for a later bounded pass
                with self._result_condition:
                    self._maintenance_runs += 1
                    self._result_condition.notify_all()
                next_maintenance = time.monotonic() + 5
            is_job = False
            try:
                item = self._jobs.get_nowait()
                is_job = True
            except queue.Empty:
                if upper is None and self._rescan.is_set():
                    self._rescan.clear(); upper, cursor = utcnow(), None
                if upper is not None and not backlog:
                    try:
                        rows = self._recovery_page(cursor, upper)
                    except Exception:
                        self._rescan.set(); upper = None
                        time.sleep(0.25)
                        continue
                    if rows:
                        cursor = (rows[-1][0], rows[-1][1])
                        backlog.extend(rows)
                    else:
                        upper = None
                if backlog:
                    _, event_key, saved, digest = backlog.popleft()
                    raw = bytes(saved)
                    if sha256(raw).hexdigest() != digest:
                        with self._result_condition:
                            self._recovered[event_key] = {'outcome':'processing_failed'}
                            self._result_condition.notify_all()
                        continue
                    item = (None, raw, hmac.new(self._secret, raw, sha256).hexdigest(), event_key)
                else:
                    try:
                        item = self._jobs.get(timeout=0.25)
                        is_job = True
                    except queue.Empty:
                        continue
            try:
                if item is None:
                    return
                ticket, raw, signature, event_key = item
                tenant, account = self._scope
                try:
                    result = self._worker.handle(tenant, account, raw_body=raw, signature=signature,
                                                 secret=self._secret, event_key=event_key)
                except Refused:
                    result = {'outcome':'deferred'}
                except Exception:
                    result = {'outcome':'processing_failed'}
                with self._result_condition:
                    if ticket is not None:
                        self._results[ticket] = result
                    else:
                        self._recovered[event_key] = result
                    for values in (self._results, self._recovered):
                        while len(values) > 128:
                            values.pop(next(iter(values)))
                    self._result_condition.notify_all()
            finally:
                if is_job:
                    self._jobs.task_done()

    def wait_maintenance(self, timeout=10):
        if type(timeout) not in (int, float) or not 0 < timeout <= 30:
            raise Refused('invalid local maintenance timeout')
        end = time.monotonic() + timeout
        with self._result_condition:
            while not self._maintenance_runs:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise Refused('local recovery maintenance has not run')
                self._result_condition.wait(remaining)

    def wait_recovery(self, event_key, timeout=10):
        if type(event_key) is not str or type(timeout) not in (int, float) or not 0 < timeout <= 30:
            raise Refused('invalid local recovery receipt')
        end = time.monotonic() + timeout
        with self._result_condition:
            while event_key not in self._recovered:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise Refused('local recovery outcome is not yet available')
                self._result_condition.wait(remaining)
            return self._recovered.pop(event_key)

    def wait(self, ticket, timeout=10):
        if type(ticket) is not int or type(timeout) not in (int, float) or not 0 < timeout <= 30:
            raise Refused('invalid local processing receipt')
        end = time.monotonic() + timeout
        with self._result_condition:
            while ticket not in self._results:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise Refused('local processing outcome is not yet available')
                self._result_condition.wait(remaining)
            return self._results.pop(ticket)

    def recover(self, event_key):
        # Explicit restart recovery reads the already authenticated, immutable
        # webhook from the currently bound epoch; it never trusts new raw bytes.
        from sqlalchemy import select
        from sqlalchemy.orm import Session
        from db.onlyfans_engine_models import OfFansConnection, OfFansWebhook
        if self._worker is None or type(event_key) is not str:
            raise Refused('local recovery scope is unavailable')
        tenant, account = self._scope
        with Session(self._worker.engine.store.engine) as s, s.begin():
            runtime = self._worker.engine.store._runtime(s, tenant, account)
            connection = s.scalar(select(OfFansConnection).where(OfFansConnection.account_id == account,
                OfFansConnection.binding_id == runtime.binding_id, OfFansConnection.status == 'active'))
            row = s.get(OfFansWebhook, (account, connection.epoch, event_key)) if connection else None
            if row is None or row.status != 'accepted' or sha256(row.raw_body).hexdigest() != row.payload_sha256:
                raise Refused('durable local webhook evidence is unavailable')
            raw = bytes(row.raw_body)
        signature = hmac.new(self._secret, raw, sha256).hexdigest()
        ticket = self._enqueue(raw, signature, event_key)
        if ticket is None:
            raise Refused('local processing queue is full')
        return ticket

    def close(self):
        if self._processor is not None and self._processor.is_alive():
            # Remaining queued events stay durable for the next startup sweep.
            self._stopping.set()
            try:
                self._jobs.put(None, timeout=1)
            except queue.Full:
                pass
            self._processor.join(timeout=5)
        if self._thread.is_alive():
            self._server.shutdown()
            self._thread.join(timeout=3)
        self._server.server_close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class FansApiLocalHttpTransport(FansApiSyntheticTransport):
    """Only talks to the exact in-process lab on IPv4 loopback, never a URL."""
    def __init__(self, store, tenant_id, account_id, lab):
        if type(lab) is not LocalFansHttpLab or type(lab.provider) is not SyntheticFansProvider:
            raise Refused('only the local synthetic HTTP lab is allowed')
        super().__init__(store, tenant_id, account_id, lab.provider)
        self.lab = lab

    def _send_provider(self, request):
        if (type(self.lab) is not LocalFansHttpLab or self.provider is not self.lab.provider
                or type(self.provider) is not SyntheticFansProvider
                or not _ACCOUNT.fullmatch(request['account_id']) or not _CHAT.fullmatch(request['chat_id'])):
            raise Refused('local synthetic transport scope is invalid')
        path = '/api/' + request['account_id'] + '/chats/' + request['chat_id'] + '/messages'
        connection = HTTPConnection('127.0.0.1', self.lab.port, timeout=3)
        try:
            connection.request('POST', path, body=_json_bytes(request['body']), headers={
                'Content-Type': 'application/json', 'Idempotency-Key': request['idempotency_key'],
                'X-Synthetic-Token': self.lab._token})
            response = connection.getresponse()
            data = response.read(_MAX_BODY + 1)
            if len(data) > _MAX_BODY:
                return None, None
            try:
                body = json.loads(data.decode('utf-8'), object_pairs_hook=_unique_object, parse_float=Decimal, parse_constant=_nonfinite)
            except (UnicodeError, ValueError, TypeError):
                return response.status, None
            return response.status, body
        finally:
            connection.close()
