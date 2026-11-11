"""History, transaction and chargeback read failures are classified like the vault importer's.

Mocked wire only, no database. Before this change these three reads returned a bare 'unavailable' result with
no failure class, so a 429 with Retry-After could not be told from a schema error or a rejected credential.
"""
import httpx
import pytest
from test_engine_real_worker_pg import answer
from fans_api_adapter.real_transport import RealFansConfig, build_client

ACCOUNT, CREATOR, FAN = "acct_cls_1", "301", 401
BASE = "https://api.fansapi.com"


def client_for(wire):
    def handler(req):
        if req.url.path == "/api/accounts":
            return answer(200, [{"id": ACCOUNT, "onlyfans_id": CREATOR, "is_authenticated": True}])
        return wire(req)
    return build_client(RealFansConfig("synthetic-token", ACCOUNT, BASE, 5), creator_id=CREATOR, epoch=1,
                        transport=httpx.MockTransport(handler))


def reads(client):
    return {"history": lambda **k: client.history(str(FAN), limit=100, **k),
            "transactions": lambda **k: client.transactions(limit=100, start_date="2016-01-01 00:00:00", **k),
            "chargebacks": lambda **k: client.chargebacks(limit=100, start_date="2016-01-01 00:00:00", **k)}


@pytest.mark.parametrize("read", ["history", "transactions", "chargebacks"])
@pytest.mark.parametrize("status,headers,failure,cause,retry", [
    (429, {"Retry-After": "120"}, "transient", "provider_rate_limited", 120),
    (429, {}, "transient", "provider_rate_limited", None),
    (503, {"Retry-After": "7"}, "transient", "provider_unavailable", 7),
    (500, {}, "transient", "provider_unavailable", None),
    (408, {}, "transient", "provider_unavailable", None),
    (401, {"Retry-After": "30"}, "auth", "provider_auth_rejected", None),
    (403, {}, "auth", "provider_forbidden", None),
    (404, {}, "schema", "provider_response_invalid", None),
])
def test_failed_status_is_classified_with_retry_after(read, status, headers, failure, cause, retry):
    client = client_for(lambda req: answer(status, {"error": "synthetic"}, headers))
    result = reads(client)[read]()
    assert result.state == "unavailable" and result.http_status == status
    assert (result.failure, result.cause, result.retry_after) == (failure, cause, retry)
    assert client.last_failure is result


@pytest.mark.parametrize("read", ["history", "transactions", "chargebacks"])
def test_unreachable_provider_is_transient(read):
    def boom(req):
        raise TimeoutError("synthetic")
    result = reads(client_for(boom))[read]()
    assert (result.failure, result.cause) == ("transient", "provider_unreachable")


def test_200_with_an_invalid_shape_is_a_schema_failure_not_a_transient_one():
    cases = {
        "history": [{"data": "x"}, {"data": [], "_pagination": {}}, {"data": [{}] * 101, "_pagination": {"next_page": None}}],
        "transactions": [{"data": {"list": "x", "hasMore": False}}, {"data": {"list": [], "hasMore": "no"}}, {"nope": 1},
                         {"data": {"list": [{"id": "!"}], "hasMore": False}}, {"data": {"list": [], "hasMore": True, "nextMarker": ""}}],
    }
    for read, bodies in cases.items():
        for body in bodies:
            result = reads(client_for(lambda req, body=body: answer(200, body)))[read]()
            assert (result.state, result.failure, result.cause, result.retry_after) == (
                "unavailable", "schema", "provider_response_invalid", None), (read, body)
    result = reads(client_for(lambda req: httpx.Response(200, headers={"content-length": "8"}, stream=httpx.ByteStream(b"not json"))))["chargebacks"]()
    assert (result.state, result.failure, result.cause) == ("unavailable", "schema", "provider_response_invalid")


def test_non_advancing_history_continuation_is_a_schema_failure():
    url = f"{BASE}/api/{ACCOUNT}/chats/{FAN}/messages?limit=100&order=desc"

    def wire(req):
        return answer(200, {"data": [], "_pagination": {"next_page": str(req.url)}})
    result = client_for(wire).history(str(FAN), limit=100, next_page=url)
    assert (result.failure, result.cause) == ("schema", "provider_response_invalid")


def test_successful_reads_are_unchanged_and_carry_no_failure():
    history = client_for(lambda req: answer(200, {"data": [{"id": 1}], "_pagination": {"next_page": None}})).history(str(FAN), limit=100)
    assert history.state == "observed" and history.failure is None and history.data == ({"id": 1},)
    tx = client_for(lambda req: answer(200, {"data": {"list": [{"id": "t1"}], "hasMore": False}})).transactions(limit=100)
    assert tx.state == "observed" and tx.failure is None and tx.next_page is None
    cb = client_for(lambda req: answer(200, {"data": {"list": []}})).chargebacks(limit=100)
    assert cb.state == "unverified_observation" and cb.failure is None


def test_strict_row_validation_is_default_and_lenient_rows_pass_malformed_rows_through():
    bad_history = {"data": [{"id": 1}, {"text": "no id"}, "not even an object"], "_pagination": {"next_page": None}}
    strict = client_for(lambda req: answer(200, bad_history)).history(str(FAN), limit=100)
    assert strict.state == "unavailable" and strict.failure == "schema"
    lenient = client_for(lambda req: answer(200, bad_history)).history(str(FAN), limit=100, lenient_rows=True)
    assert lenient.state == "observed" and len(lenient.data) == 3
    bad_tx = {"data": {"list": [{"id": "t1"}, {"amount": 1}, "x"], "hasMore": False}}
    assert client_for(lambda req: answer(200, bad_tx)).transactions(limit=100).failure == "schema"
    lenient_tx = client_for(lambda req: answer(200, bad_tx)).transactions(limit=100, lenient_rows=True)
    assert lenient_tx.state == "observed" and len(lenient_tx.data) == 3
    # the page bound and continuation are still enforced when rows are lenient
    over = {"data": [{}] * 101, "_pagination": {"next_page": None}}
    assert client_for(lambda req: answer(200, over)).history(str(FAN), limit=100, lenient_rows=True).failure == "schema"
