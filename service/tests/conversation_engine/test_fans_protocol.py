"""Real protocol shapes, dummy credentials and injected in-process HTTP only."""
import hashlib
import hmac
import json
import unittest
from decimal import Decimal
from fans_api_adapter.protocol import FansApiClient, Response


class FakeHTTP:
    def __init__(self, responses): self.responses, self.calls = list(responses), []
    def __call__(self, **request):
        self.calls.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception): raise item
        return item


def response(status, value, headers=None):
    return Response(status, json.dumps(value).encode(), headers or {})


def client(http, **kw):
    result = FansApiClient(account='acct_test', creator_id='123', epoch=2, token='fixture-token',
                         http=http, timeout=2, enabled=True, **kw)
    # Ordinary unit scenarios focus on the later mapping; identity is tested separately.
    result._identity_verified = True
    return result


class ProtocolTests(unittest.TestCase):
    def test_explicit_configuration_and_identity_scope(self):
        fake = FakeHTTP([response(200, [{'id':'acct_test','onlyfans_id':123,'is_authenticated':True}]),
                         response(200, [{'id':'acct_test','onlyfans_id':999,'is_authenticated':True}])])
        c = client(fake)
        self.assertEqual(c.identity().state, 'verified')
        self.assertEqual(c.identity().state, 'identity_mismatch')
        self.assertEqual(fake.calls[0]['url'], 'https://app.onlyfansapi.com/api/accounts')
        self.assertEqual(fake.calls[0]['headers']['Authorization'], 'Bearer fixture-token')
        self.assertIs(fake.calls[0]['follow_redirects'], False)
        with self.assertRaises(ValueError):
            FansApiClient(account='acct_test', creator_id='123', epoch=2, token='fixture-token', http=fake, timeout=2)

    def test_unverified_identity_cannot_send_or_read(self):
        fake=FakeHTTP([])
        c=FansApiClient(account='acct_test', creator_id='123', epoch=2, token='fixture-token', http=fake, timeout=2, enabled=True)
        with self.assertRaises(ValueError): c.send(101,key='stable',text='hello')
        with self.assertRaises(ValueError): c.history(101)
        self.assertEqual(fake.calls, [])

    def test_history_follows_scoped_continuations_and_short_pages(self):
        url='https://app.onlyfansapi.com/api/acct_test/chats/101/messages?first_id=9'
        fake=FakeHTTP([response(200, {'data':[{'id':9}], '_pagination':{'next_page':url}}),
                       response(200, {'data':[], '_pagination':{'next_page':None}})])
        c=client(fake); first=c.history(101, limit=20)
        self.assertEqual(first.state,'observed'); self.assertEqual(first.next_page,url)
        self.assertEqual(c.history(101, limit=20, next_page=first.next_page).next_page,None)
        self.assertEqual(len(fake.calls),2)
        for bad in ['https://evil.invalid/api/acct_test/chats/101/messages',
                    'https://app.onlyfansapi.com/api/acct_other/chats/101/messages',
                    'https://app.onlyfansapi.com/api/acct_test/chats/102/messages',
                    'https://app.onlyfansapi.com/api/acct_test/chats/101/messages?limit=100000']:
            with self.assertRaises(ValueError): c.history(101, next_page=bad)
        self.assertEqual(len(fake.calls),2)

    def test_wire_price_and_one_post(self):
        fake=FakeHTTP([response(200, {'data':{'id':987,'createdAt':'2026-11-07T00:00:00Z'}}, {'x-ofapi-trace-id':'trace-1','X-OFAPI-Idempotency-Original-Trace-Id':'original-1'})])
        c=client(fake); result=c.send(101,key='stable-key',text='Offer',media=[11,12],previews=[11],price_cents=1001)
        self.assertEqual(result.state,'accepted'); self.assertEqual(result.data['message_id'],'987')
        self.assertEqual((result.http_status, result.trace_id, result.original_trace_id), (200, 'trace-1', 'original-1'))
        call=fake.calls[0]; body=json.loads(call['body'],parse_float=Decimal)
        self.assertEqual(body, {'text':'Offer','mediaFiles':['11','12'],'previews':['11'],'price':Decimal('10.01')})
        self.assertEqual(call['headers']['Idempotency-Key'],'stable-key')
        self.assertEqual(call['method'],'POST'); self.assertEqual(len(fake.calls),1)

    def test_provider_media_ids_and_malformed_timestamp(self):
        fake=FakeHTTP([response(200, {'data':{'id':987,'createdAt':'not-a-time'}})])
        result=client(fake).send(101,key='stable',text='Hi',media=['ofapi_media_abc'],previews=['ofapi_media_abc'])
        self.assertEqual(result.state, 'unknown')
        self.assertEqual(json.loads(fake.calls[0]['body'])['mediaFiles'], ['ofapi_media_abc'])

    def test_send_ambiguous_results_never_retry(self):
        cases=[(TimeoutError(), 'unknown'), (response(429,{}),'unknown'), (response(500,{}),'unknown'),
               (response(200,{'data':{'id':1}}),'unknown'),
               (response(503,{'error':'SERVICE_UNAVAILABLE','request_outcome':'not_sent'}),'not_sent'),
               (response(422,{'error':'IDEMPOTENCY_KEY_MISMATCH'}),'key_mismatch'),
               (response(409,{'error':'IDEMPOTENCY_CONFLICT'}),'conflict')]
        for value, expected in cases:
            with self.subTest(expected=expected):
                fake=FakeHTTP([value]); result=client(fake).send(101,key='unchanged',text='hello')
                self.assertEqual(result.state,expected); self.assertEqual(len(fake.calls),1)

    def test_financial_observations_do_not_claim_ownership(self):
        fake=FakeHTTP([response(200,{'data':{'list':[{'id':'tx1','amount':10,'currency':'USD'}], 'hasMore':False}}),
                       response(200,{'data':[]})])
        c=client(fake); result=c.transactions()
        self.assertEqual(result.state,'observed'); self.assertIsNone(result.next_page)
        self.assertEqual(c.chargebacks().state,'unverified_observation')
        self.assertNotIn('owned',result.data[0])

    def test_transaction_opaque_marker_and_nonadvancing_page(self):
        fake=FakeHTTP([response(200, {'data':{'list':[{'id':'tx1'}], 'hasMore':True, 'nextMarker':'opaque+cursor'}}),
                       response(200, {'data':{'list':[{'id':'tx2'}], 'hasMore':True, 'nextMarker':'opaque+cursor'}})])
        c=client(fake)
        first=c.transactions()
        self.assertEqual(first.next_page, 'opaque+cursor')
        self.assertEqual(c.transactions(marker=first.next_page).state, 'unavailable')
        self.assertIn('marker=opaque%2Bcursor', fake.calls[1]['url'])
        with self.assertRaises(ValueError): c.transactions(marker='bad\nmarker')
        self.assertEqual(len(fake.calls), 2)

    def test_authenticated_webhook_scope_and_unsupported_events(self):
        c=client(FakeHTTP([])); raw=json.dumps({'account_id':'acct_test','event':'transactions.new','payload':{'id':'tx1'}}).encode()
        sig=hmac.new(b'fixture',raw,hashlib.sha256).hexdigest()
        observed=c.webhook(raw,signature=sig,secret=b'fixture',event_key='event-1')
        self.assertEqual(observed.state,'observation'); self.assertEqual(observed.data['epoch'],2)
        self.assertEqual(c.webhook(raw,signature='0'*64,secret=b'fixture',event_key='event-1').state,'unavailable')
        self.assertEqual(c.webhook(raw,signature=sig,secret=b'fixture',event_key='').state,'unavailable')
        other=json.dumps({'account_id':'acct_other','event':'transactions.new','payload':{}}).encode()
        other_sig=hmac.new(b'fixture',other,hashlib.sha256).hexdigest()
        self.assertEqual(c.webhook(other,signature=other_sig,secret=b'fixture',event_key='1').state,'scope_mismatch')


if __name__ == '__main__': unittest.main()
