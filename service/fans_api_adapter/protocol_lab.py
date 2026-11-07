"""Wire the documented protocol mapping to the existing authority in a no-network lab."""
from datetime import datetime, timezone
from decimal import Decimal
import json
from sqlalchemy import select
from sqlalchemy.orm import Session
from db.onlyfans_engine_models import OfEngineDecision, OfFansConnection
from onlyfans_runtime.store import Refused
from .protocol import FansApiClient, Response
from .synthetic_transport import FansApiSyntheticTransport, SyntheticFansProvider


class _FixtureHTTP:
    def __init__(self, provider, account, creator):
        self.provider, self.account, self.creator = provider, account, creator
    def __call__(self, *, method, url, headers, body, timeout, follow_redirects):
        if follow_redirects is not False or headers.get('Authorization') != 'Bearer fixture-token':
            raise Refused('fixture transport scope')
        if method == 'GET' and url == 'https://app.onlyfansapi.com/api/accounts':
            return Response(200, json.dumps([{'id':self.account,'onlyfans_id':self.creator,'is_authenticated':True}]).encode())
        if (method != 'POST' or follow_redirects is not False or headers.get('Authorization') != 'Bearer fixture-token'
                or not url.startswith('https://app.onlyfansapi.com/api/')):
            raise Refused('fixture transport scope')
        suffix = url.removeprefix('https://app.onlyfansapi.com/api/').split('/')
        if len(suffix) != 4 or suffix[1] != 'chats' or suffix[3] != 'messages':
            raise Refused('fixture path')
        request={'account_id':suffix[0], 'chat_id':suffix[2], 'idempotency_key':headers.get('Idempotency-Key'),
                 'body':json.loads(body, parse_float=Decimal)}
        status, data = self.provider.send(request)
        if status == 200 and type(data) is dict and type(data.get('data')) is dict:
            data = {'data':{'id':data['data'].get('id'), 'createdAt':datetime.now(timezone.utc).isoformat()}}
        return Response(status, json.dumps(data).encode(), {'x-ofapi-trace-id':'synthetic-trace'})


class FansApiProtocolLabTransport(FansApiSyntheticTransport):
    """Exact fixture type: cannot accept arbitrary HTTP, credentials or remote egress."""
    def __init__(self, store, tenant_id, account_id, provider, *, provider_account, creator_id, epoch):
        if type(provider) is not SyntheticFansProvider:
            raise Refused('fixture provider required')
        super().__init__(store, tenant_id, account_id, provider)
        self.client = FansApiClient(account=provider_account, creator_id=creator_id, epoch=epoch,
            token='fixture-token', http=_FixtureHTTP(provider, provider_account, creator_id), timeout=2, enabled=True)
        if self.client.identity().state != 'verified':
            raise Refused('fixture identity unavailable')

    def send(self, dispatch, *, authority, tenant_id):
        with Session(self.store.engine) as s, s.begin():
            runtime = self.store._runtime(s, tenant_id, self.account_id)
            decision = s.scalar(select(OfEngineDecision).where(OfEngineDecision.account_id == self.account_id,
                                                                 OfEngineDecision.intent_id == dispatch.intent_id))
            connection = s.get(OfFansConnection, (self.account_id, self.client.epoch))
            if (decision is None or decision.connection_epoch != self.client.epoch or connection is None
                    or connection.binding_id != runtime.binding_id or connection.status != 'active'
                    or connection.provider_account_id != self.client.account or connection.provider_creator_id != self.client.creator_id):
                raise Refused('protocol fixture binding changed')
        return super().send(dispatch, authority=authority, tenant_id=tenant_id)

    def _send_provider(self, request):
        if request['account_id'] != self.client.account:
            raise Refused('protocol fixture account mismatch')
        body=request['body']
        price=body.get('price', Decimal(0))
        cents=price * 100
        if cents != cents.to_integral_value():
            raise Refused('invalid fixture amount')
        result=self.client.send(request['chat_id'], key=request['idempotency_key'], text=body['text'],
            media=body.get('mediaFiles', ()), previews=body.get('previews', ()), price_cents=int(cents))
        if result.state == 'accepted':
            return 200, {'data':{'id':result.data['message_id']}, '_fixture_scope':{
                'account_id':request['account_id'], 'chat_id':request['chat_id']}}
        if result.state == 'not_sent':
            return 503, {'error':'SERVICE_UNAVAILABLE', 'request_outcome':'not_sent'}
        if result.state == 'key_mismatch': return 422, {'error':'IDEMPOTENCY_KEY_MISMATCH'}
        if result.state == 'conflict': return 409, {'error':'IDEMPOTENCY_CONFLICT'}
        return None, None
