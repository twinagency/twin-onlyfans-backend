"""FansApiClient.send keeps typed, sanitized evidence for every outcome it cannot prove. No network, no database.

Before the fix a 4xx, an ambiguous 5xx, a malformed 200 and a transport exception collapsed into Result('unknown')
with the body, exception class and error code discarded. The state names below are the EXISTING mapping and must not
change: only provider-documented not-sent outcomes may ever become 'not_sent' or 'rejected'.
"""
import json
import unittest

from fans_api_adapter.protocol import FansApiClient, Response

TOKEN = 'fixture-token-ABCDEF0123456789'


class FakeHTTP:
    def __init__(self, responses): self.responses, self.calls = list(responses), []
    def __call__(self, **request):
        self.calls.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception): raise item
        return item


def reply(status, value, headers=None):
    raw = value if isinstance(value, bytes) else json.dumps(value).encode()
    return Response(status, raw, headers or {})


def send(item, text='Hello there fan', **kw):
    c = FansApiClient(account='acct_test', creator_id='123', epoch=2, token=TOKEN, http=FakeHTTP([item]), timeout=2, enabled=True)
    c._identity_verified = True
    return c.send(101, key='stable-key', text=text, **kw)


class SendEvidenceTests(unittest.TestCase):
    def test_every_unproven_outcome_carries_typed_evidence_and_keeps_its_state(self):
        trace = {'x-ofapi-trace-id': 'trace-abc-123'}
        cases = [
            (reply(400, {'error': 'BAD_REQUEST', 'message': 'nope'}, trace), 'unknown', 'provider_rejected_4xx', 400, 'BAD_REQUEST'),
            (reply(422, {'error': 'BANNED_WORDS'}, trace), 'unknown', 'provider_rejected_4xx', 422, 'BANNED_WORDS'),
            (reply(503, {'error': 'SERVICE_UNAVAILABLE', 'request_outcome': 'unknown'}), 'unknown', 'provider_ambiguous_5xx', 503, 'SERVICE_UNAVAILABLE'),
            (reply(502, b'<html>bad gateway</html>'), 'unknown', 'provider_ambiguous_5xx', 502, None),
            (reply(200, {'data': {'id': 'not-a-number', 'createdAt': '2026-11-07T00:00:00+00:00'}}), 'unknown', 'malformed_success', 200, None),
            (reply(200, b'<<not json>>'), 'unknown', 'malformed_success', 200, None),
            (reply(409, {'error': 'IDEMPOTENCY_CONFLICT'}), 'conflict', 'provider_idempotency_conflict', 409, 'IDEMPOTENCY_CONFLICT'),
            (reply(422, {'error': 'IDEMPOTENCY_KEY_MISMATCH'}), 'key_mismatch', 'provider_key_mismatch', 422, 'IDEMPOTENCY_KEY_MISMATCH'),
        ]
        for item, state, reason, status, code in cases:
            with self.subTest(reason=reason, status=status, code=code):
                result = send(item)
                self.assertEqual(result.state, state)
                ev = result.evidence
                self.assertIsNotNone(ev)
                self.assertEqual((ev.reason, ev.http_status, ev.provider_code, ev.exception_class), (reason, status, code, None))
                self.assertEqual(ev.trace_id, result.trace_id)

    def test_trace_id_and_body_excerpt_are_kept(self):
        ev = send(reply(400, {'error': 'BAD_REQUEST', 'message': 'invalid media'}, {'x-ofapi-trace-id': 'trace-abc-123'})).evidence
        self.assertEqual(ev.trace_id, 'trace-abc-123')
        self.assertIn('invalid media', ev.body_excerpt)
        self.assertEqual(send(reply(200, b'<<not json>>')).evidence.body_excerpt, '<<not json>>')

    def test_transport_exception_keeps_only_the_class_name(self):
        ev = send(TimeoutError('read timed out sending ' + TOKEN)).evidence
        self.assertEqual((ev.reason, ev.http_status, ev.exception_class), ('transport_exception', None, 'TimeoutError'))
        self.assertNotIn(TOKEN, repr(ev))

    def test_a_transport_that_returns_the_wrong_type_is_a_transport_exception(self):
        ev = send('not a Response').evidence
        self.assertEqual(ev.reason, 'transport_exception')
        self.assertIsNotNone(ev.exception_class)

    def test_secrets_and_message_text_are_redacted_and_the_body_is_bounded(self):
        body = {'error': 'VALIDATION_FAILED', 'message': 'bad: Hello there fan | Authorization: Bearer ' + TOKEN + ' | ' + 'a' * 500}
        ev = send(reply(422, body)).evidence
        shown = repr(ev)
        self.assertNotIn(TOKEN, shown)
        self.assertNotIn('Hello there fan', shown)
        self.assertLessEqual(len(ev.body_excerpt), 160)
        raw = send(reply(502, b'secret_key=abcdef0123456789abcdef0123456789abcdef ' + b'z' * 5000)).evidence
        self.assertNotIn('abcdef0123456789', repr(raw))
        self.assertLessEqual(len(raw.body_excerpt), 160)

    def test_control_valid_success_has_no_evidence(self):
        result = send(reply(200, {'data': {'id': 9001, 'createdAt': '2026-11-07T00:00:00+00:00'}}))
        self.assertEqual((result.state, result.data['message_id']), ('accepted', '9001'))
        self.assertIsNone(getattr(result, 'evidence', None))

    def test_control_explicit_provider_not_sent_keeps_its_existing_state(self):
        rejected = send(reply(422, {'error': 'BANNED_WORDS', 'request_outcome': 'not_sent'}))
        self.assertEqual(rejected.state, 'rejected')
        self.assertIsNone(getattr(rejected, 'evidence', None))
        self.assertEqual(send(reply(400, {'error': 'X', 'request_outcome': 'not_sent'})).state, 'rejected')
        self.assertEqual(send(reply(503, {'error': 'SERVICE_UNAVAILABLE', 'request_outcome': 'not_sent'})).state, 'not_sent')
        self.assertEqual(send(reply(503, {'error': 'IDEMPOTENCY_UNAVAILABLE'})).state, 'not_sent')
        self.assertIsNone(getattr(send(reply(503, {'error': 'SERVICE_UNAVAILABLE', 'request_outcome': 'not_sent'})), 'evidence', None))

    def test_a_4xx_is_still_not_proof_of_non_delivery(self):
        for status in (400, 401, 403, 404, 422, 429):
            with self.subTest(status=status):
                self.assertEqual(send(reply(status, {'error': 'SOMETHING'})).state, 'unknown')

    def test_encoded_evidence_fits_the_column_even_with_every_field_at_its_cap(self):
        from fans_api_adapter.send_outcome import SendEvidence, encode_evidence
        ev = SendEvidence(reason='provider_idempotency_conflict', http_status=409, provider_code='C' * 200,
                          request_outcome='o' * 99, trace_id='t' * 500, exception_class='E' * 300, body_excerpt='b' * 5000)
        encoded = encode_evidence(ev)
        self.assertLessEqual(len(encoded), 256)
        self.assertEqual(json.loads(encoded.split(':', 1)[1])['reason'], 'provider_idempotency_conflict')


if __name__ == '__main__': unittest.main()
