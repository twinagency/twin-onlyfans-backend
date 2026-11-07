"""All HTTP uses httpx MockTransport; no provider or model call is made."""
import httpx
import json
import pytest
from fans_api_adapter.protocol import FansApiClient
from fans_api_adapter.https_transport import FansHttpsTransport
from conversation_engine.model_contract import parse_proposal
from onlyfans_runtime.store import Refused

class Stream(httpx.SyncByteStream):
    def __init__(self, content):
        self.content = content
    def __iter__(self):
        yield self.content


def test_https_transport_and_client_with_in_process_mock():
    calls = []
    def handler(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer fixture"
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, stream=Stream(json.dumps([{"id":"acct_test","onlyfans_id":123,"is_authenticated":True}]).encode()))
    wire = FansHttpsTransport(enabled=True, transport=httpx.MockTransport(handler))
    client = FansApiClient(account="acct_test", creator_id="123", epoch=1, token="fixture",
                           http=wire, timeout=1, enabled=True)
    assert client.identity().state == "verified"
    assert len(calls) == 1


def test_https_transport_limits_and_redirects():
    with pytest.raises(ValueError):
        FansHttpsTransport()
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"location":"https://elsewhere.invalid/"}, stream=Stream(b""))
    wire = FansHttpsTransport(enabled=True, transport=httpx.MockTransport(handler))
    result = wire(method="GET", url="https://app.onlyfansapi.com/api/accounts", headers={},
                  body=None, timeout=1, follow_redirects=False)
    assert result.status == 302 and len(seen) == 1
    with pytest.raises(ValueError):
        wire(method="GET", url="https://elsewhere.invalid/api/accounts", headers={},
             body=None, timeout=1, follow_redirects=False)
    def oversized(_):
        return httpx.Response(200, stream=Stream(b"x" * 1_000_001))
    wire = FansHttpsTransport(enabled=True, transport=httpx.MockTransport(oversized))
    with pytest.raises(ValueError):
        wire(method="GET", url="https://app.onlyfansapi.com/api/accounts", headers={},
             body=None, timeout=1, follow_redirects=False)


def test_model_contract_does_not_accept_unknown_terms_or_ambiguous_json():
    assert parse_proposal('{"body":"Hello.","parts":["How are you?"]}').parts == ("How are you?",)
    for raw in ('{"body":"a","body":"b"}', '{"body":"a","price":10}',
                '{"body":"a","parts":"b"}', '{"body":"a","item_scope":NaN}', '[]'):
        with pytest.raises(Refused):
            parse_proposal(raw)
