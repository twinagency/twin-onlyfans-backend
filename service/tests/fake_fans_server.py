"""Loopback fake of the Fans API used by the real-transport tests (no network egress)."""
import os
import json
import hashlib
import hmac
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import httpx

ACCT, CREATOR = 'acct_real_fixture', os.environ.get('LIVE_TEST_CREATOR_OF_ID', '900000000001')


class FakeFans:
    def __init__(self, outcomes=None, vault=None):
        self.outcomes = list(outcomes or [])   # per POST: accepted|not_sent|unknown|conflict|banned|mismatch|lost
        self.vault = vault or []
        self.posts, self.executions, self.stored = [], {}, {}
        self.next_id = 7001
        self.webhook_secret = b'fake-fans-webhook-secret'   # signs webhooks (sign_webhook), like the provider does
        self.transactions = []     # rows served by GET transactions (empty = a fan with no payment history)
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def _json(self, status, obj, headers=None):
                raw = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(raw)))
                for k, v in (headers or {}).items(): self.send_header(k, v)
                self.end_headers(); self.wfile.write(raw)
            def do_GET(self):
                if self.path == '/api/accounts':
                    return self._json(200, [{'id': ACCT, 'onlyfans_id': CREATOR, 'is_authenticated': True}])
                if self.path.startswith(f'/api/{ACCT}/media/vault/lists'):
                    return self._json(200, {'data': {'list': [{'id': 5, 'type': 'custom', 'name': 'L', 'photosCount': 1, 'videosCount': 0}]}})
                if self.path.startswith(f'/api/{ACCT}/media/vault?'):
                    q = dict(p.split('=') for p in self.path.split('?')[1].split('&'))
                    off, lim = int(q['offset']), int(q['limit'])
                    page = fake.vault[off:off + lim]
                    return self._json(200, {'data': {'list': page, 'hasMore': off + lim < len(fake.vault)}})
                path = self.path.split('?')[0]
                if path == f'/api/{ACCT}/transactions':
                    return self._json(200, {'data': {'list': list(fake.transactions), 'hasMore': False}})
                if path == f'/api/{ACCT}/chargebacks':
                    return self._json(200, {'data': {'list': []}})
                if path.startswith(f'/api/{ACCT}/chats/') and path.endswith('/messages'):
                    return self._json(200, {'data': [], '_pagination': {'next_page': None}})   # a new fan: empty history
                self._json(404, {})
            def do_POST(self):
                raw = self.rfile.read(int(self.headers['Content-Length']))
                body = json.loads(raw)
                key = self.headers.get('Idempotency-Key')
                fake.posts.append({'path': self.path, 'key': key, 'body': body, 'auth': self.headers.get('Authorization')})
                if key in fake.stored:
                    return self._json(200, fake.stored[key], {'Idempotent-Replayed': 'true'})
                outcome = fake.outcomes.pop(0) if fake.outcomes else 'accepted'
                if outcome == 'not_sent':
                    return self._json(503, {'error': 'SERVICE_UNAVAILABLE', 'request_outcome': 'not_sent'})
                if outcome == 'unknown':
                    return self._json(503, {'error': 'SERVICE_UNAVAILABLE', 'request_outcome': 'unknown'})
                if outcome == 'conflict':
                    return self._json(409, {'error': 'IDEMPOTENCY_CONFLICT'})
                if outcome == 'banned':
                    return self._json(422, {'error': 'BANNED_WORDS', 'message': 'banned word'})
                if outcome == 'mismatch':
                    return self._json(422, {'error': 'IDEMPOTENCY_KEY_MISMATCH'})
                fake.executions[key] = fake.executions.get(key, 0) + 1
                resp = {'data': {'id': fake.next_id, 'createdAt': '2026-11-07T00:00:00+00:00'}}
                fake.next_id += 1
                fake.stored[key] = resp
                if outcome == 'lost':
                    self.connection.shutdown(2); self.connection.close(); return
                self._json(200, resp)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def sign_webhook(self, body, key=None):
        '''(raw bytes, hex HMAC-SHA256 signature, event key) for a webhook body: the provider's normal signing path.'''
        raw = json.dumps(body, separators=(',', ':')).encode()
        signature = hmac.new(self.webhook_secret, raw, hashlib.sha256).hexdigest()
        return raw, signature, key or 'evt-' + hashlib.sha256(raw).hexdigest()[:24]

    def transport(self):
        port = self.port
        class Rewrite(httpx.BaseTransport):
            inner = httpx.HTTPTransport(retries=0)
            def handle_request(self, request):
                request.url = request.url.copy_with(scheme='http', host='127.0.0.1', port=port)
                return self.inner.handle_request(request)
        return Rewrite()

    def close(self):
        self.server.shutdown(); self.server.server_close()
