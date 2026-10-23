"""Role isolation at real legacy call boundaries, using fake HTTP only.

The positive control exercises the ordinary unset-role client. Negative cases
select a dedicated role after construction, including a cached client and a
new automation kind, so constructor refusal cannot hide a missing egress gate.
"""
import ast
import asyncio
import os
from pathlib import Path
import subprocess
import sys

import pytest


_SERVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SERVICE))
import of_client as provider

ROLES = ["api", "worker", "invalid-role", ""]
REFUSAL = "Live OFClient is unavailable in dedicated OnlyFans process roles"
_SESSION = {
    "headers": {"user_id": "101", "x_bc": "synthetic", "x_of_rev": "synthetic", "user_agent": "synthetic"},
    "signing": {"rules": {"static_param": "synthetic", "start": "synthetic", "end": "synthetic",
                           "checksum_indexes": [0], "checksum_constant": 0}},
}


class Response:
    ok = True
    status_code = 200
    headers = {"etag": '"synthetic"'}
    text = '{"id":9001}'
    url = "synthetic://no-network"

    def json(self):
        return {"id": 9001, "ETag": "synthetic", "processId": "synthetic", "host": "synthetic"}


class Probe:
    def __init__(self):
        self.sessions = []
        self.calls = []
        self.on_create = None
        self.on_put = None

    def factory(self, **kwargs):
        probe = self

        class FakeHttp:
            proxies = {}

            def __init__(self):
                self.cookies = type("Cookies", (), {"set": lambda *a, **kw: None})()

            def request(self, method, url, **kw):
                probe.calls.append((method, url))
                return Response()

            def get(self, url, **kw):
                return self.request("GET", url, **kw)

            def post(self, url, **kw):
                return self.request("POST", url, **kw)

            def put(self, url, **kw):
                probe.calls.append(("PUT", url))
                if probe.on_put:
                    probe.on_put()
                return Response()

            def close(self):
                pass

        session = FakeHttp()
        self.sessions.append(session)
        if self.on_create:
            self.on_create()
        return session


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("ONLYFANS_PROCESS_ROLE", raising=False)
    probe = Probe()
    monkeypatch.setattr(provider.requests, "Session", probe.factory)
    # Converter construction is local too; even its multipart object is fake.
    import curl_cffi

    class Mime:
        def addpart(self, **kw):
            pass

    monkeypatch.setattr(curl_cffi, "CurlMime", Mime)
    value = provider.OFClient(_SESSION)
    value._x_hash = "synthetic"
    probe.sessions.clear()
    return value, probe


def refused(call):
    with pytest.raises(RuntimeError, match=REFUSAL):
        call()


@pytest.mark.parametrize("role", ROLES)
def test_fresh_constructor_and_factories_refuse_before_state_access(client, monkeypatch, role):
    _, probe = client
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", role)

    def state_access(*a, **kw):
        raise AssertionError("session/account state was accessed")

    monkeypatch.setattr(provider.account_registry, "latest_session_path", state_access)
    monkeypatch.setattr(provider.account_registry, "get_active_account_id", state_access)
    refused(lambda: provider.OFClient({}))
    refused(lambda: provider.OFClient.from_session_file("synthetic-missing-session.json"))
    refused(lambda: provider.OFClient.from_account("synthetic"))
    refused(provider.OFClient.from_latest_session)
    assert probe.calls == [] and probe.sessions == []


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("verb", ["POST", "PUT", "PATCH", "DELETE"])
def test_preconstructed_generic_writes_refuse_before_signing(client, monkeypatch, role, verb):
    value, probe = client
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", role)

    def signing(*a, **kw):
        raise AssertionError("signing/hash lookup was reached")

    monkeypatch.setattr(value, "_signed_headers", signing)
    refused(lambda: value.request(verb, "/api2/v2/synthetic", json_body={}))
    assert probe.calls == [] and probe.sessions == []


@pytest.mark.parametrize("role", ROLES)
def test_preconstructed_dm_and_converter_refuse(client, monkeypatch, role):
    value, probe = client
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", role)
    refused(lambda: value.send_message(202, "Synthetic text", auto_tag=False))
    refused(lambda: value.convert_register(s3_etag="synthetic", s3_location="synthetic://object",
                                          s3_key="synthetic", s3_bucket="synthetic", filename="synthetic.jpg"))
    assert probe.calls == [] and probe.sessions == []


@pytest.mark.parametrize("role", ROLES)
def test_proxy_retry_rechecks_role_after_first_attempt(client, monkeypatch, role):
    value, probe = client
    attempts = []
    monkeypatch.setattr(provider.time, "sleep", lambda seconds: None)

    def first_attempt():
        attempts.append(True)
        monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", role)
        raise provider.requests.exceptions.ProxyError("synthetic proxy failure")

    refused(lambda: value._proxy_retry(first_attempt, tries=3))
    assert attempts == [True]
    assert probe.calls == []


def multipart_setup(value, monkeypatch, tmp_path, signed):
    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"1234")
    monkeypatch.setattr(provider, "_PART_SIZE", 2)
    monkeypatch.setattr(provider, "_PART_CONCURRENCY", 1)
    monkeypatch.setattr(provider.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(value, "request_signed_upload", signed)
    monkeypatch.setattr(value, "_upload_args", lambda: {})
    return source


def signed_parts():
    return {"keys": [{"part": 1, "putUrl": "synthetic://part-1"},
                     {"part": 2, "putUrl": "synthetic://part-2"}],
            "uploadId": "synthetic", "getUrl": "synthetic://object"}


@pytest.mark.parametrize("role", ROLES)
def test_multipart_role_change_before_part_refuses_session_construction(client, monkeypatch, tmp_path, role):
    value, probe = client

    def signed(**kwargs):
        monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", role)
        return signed_parts()

    source = multipart_setup(value, monkeypatch, tmp_path, signed)
    refused(lambda: value.upload_media(source, check_dedupe=False))
    assert probe.sessions == [] and probe.calls == []


@pytest.mark.parametrize("role", ROLES)
def test_multipart_role_change_during_part_session_creation_blocks_put(client, monkeypatch, tmp_path, role):
    value, probe = client
    source = multipart_setup(value, monkeypatch, tmp_path, lambda **kw: signed_parts())
    probe.on_create = lambda: monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", role)
    refused(lambda: value.upload_media(source, check_dedupe=False))
    assert len(probe.sessions) == 1
    assert probe.calls == []


@pytest.mark.parametrize("role", ROLES)
def test_multipart_retry_rechecks_role(client, monkeypatch, tmp_path, role):
    value, probe = client
    source = multipart_setup(value, monkeypatch, tmp_path, lambda **kw: signed_parts())

    def first_put():
        monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", role)
        raise provider.requests.exceptions.ProxyError("synthetic part failure")

    probe.on_put = first_put
    refused(lambda: value.upload_media(source, check_dedupe=False))
    assert len(probe.sessions) == 1
    assert len(probe.calls) == 1 and probe.calls[0][0] == "PUT"


@pytest.mark.parametrize("role", ROLES)
def test_new_automation_kind_cannot_use_preconstructed_cached_client(client, monkeypatch, role):
    import automation_registry
    import client_pool

    value, probe = client
    monkeypatch.setattr(automation_registry, "_REGISTRY", {})
    monkeypatch.setattr(client_pool, "_POOL", {"synthetic": value})

    @automation_registry.register("synthetic_unconverted_kind")
    async def unconverted(account_id, payload, *, run_id):
        return client_pool.get(account_id).send_message(202, "Synthetic text", auto_tag=False)

    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", role)
    refused(lambda: asyncio.run(automation_registry.get_automation("synthetic_unconverted_kind")(
        "synthetic", {}, run_id=1)))
    assert probe.calls == [] and probe.sessions == []


def test_unset_role_preserves_legacy_fake_transport_behavior(client, monkeypatch, tmp_path):
    value, probe = client
    for verb in ("POST", "PUT", "PATCH", "DELETE"):
        assert value.request_json(verb, "/api2/v2/synthetic", json_body={})["id"] == 9001
    assert value.send_message(202, "Synthetic text", auto_tag=False)["id"] == 9001
    assert value.convert_register(s3_etag="synthetic", s3_location="synthetic://object",
                                  s3_key="synthetic", s3_bucket="synthetic", filename="synthetic.jpg")["id"] == 9001
    source = multipart_setup(value, monkeypatch, tmp_path, lambda **kw: signed_parts())
    assert value.upload_media(source, check_dedupe=False)["ready"] is True
    assert [verb for verb, _ in probe.calls].count("PUT") == 3
    assert len(probe.sessions) == 2  # one separate fake session per multipart part


_IMPORT_CHECK = r"""
import importlib, importlib.abc, sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root))
forbidden = {"server", "of_client", "of_ws", "client_pool", "automation_executor", "automation_registry",
             "automations", "session_bootstrap", "webhook_dispatch", "transaction_ingest", "vault_upload",
             "fansly_backend", "llm_client", "accounts", "proxies", "live_rev", "curl_cffi"}
class NoLegacyImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        name = fullname.removeprefix("service.").split(".")[0]
        if name in forbidden:
            raise AssertionError("unexpected legacy dependency: " + fullname)
sys.meta_path.insert(0, NoLegacyImports())
import onlyfans_app
import onlyfans_runtime.worker
import onlyfans_runtime.native_worker
import onlyfans_runtime.native_connector
import onlyfans_runtime.native_protocol
import onlyfans_runtime.text_delivery
import onlyfans_runtime.configuration
import onlyfans_runtime.launch_personality.policy
from model_selection import explicit_model
assert explicit_model.__module__ == "model_selection"
if (root / "onlyfans_runtime" / "loopback_transport.py").is_file():
    import onlyfans_runtime.loopback_transport
for route in onlyfans_app.app.routes:
    methods = set(getattr(route, "methods", set()))
    if route.path.startswith("/onlyfans/control/v1/"):
        assert methods == {"POST"}, route.path
    elif route.path == "/onlyfans/content/v1/accounts/{account_id}/vault/videos/{media_id}/stream":
        assert methods == {"GET", "HEAD"}, route.path
    elif (route.path == "/onlyfans/content-sync/v1/accounts/{account_id}/drafts/{draft_id}/files/{file_id}/chunks/{index}"):
        assert methods == {"PUT"}, route.path
    elif route.path.startswith(("/onlyfans/configuration/v1/", "/onlyfans/campaign/v1/",
                                "/onlyfans/content/v1/", "/onlyfans/content/v2/",
                                "/onlyfans/offer-import/v1/", "/onlyfans/content-sync/v1/",
                                "/onlyfans/connection/v1/")):
        assert methods in ({"GET"},{"POST"}), route.path
    else:
        assert methods <= {"GET", "HEAD"}, route.path
"""


def test_dedicated_app_and_runtime_do_not_import_legacy_writers():
    env = {**os.environ, "ONLYFANS_PROCESS_ROLE": "api",
           "DATABASE_URL": "postgresql://oftest@127.0.0.1:1/synthetic"}
    result = subprocess.run([sys.executable, "-c", _IMPORT_CHECK, str(_SERVICE)],
                            env=env, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr or result.stdout


@pytest.mark.parametrize("role", ROLES)
def test_legacy_server_import_is_refused_before_dependencies(role):
    script = r"""
import importlib.abc,sys
sys.path.insert(0,sys.argv[1])
class NoLegacyDependencies(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.removeprefix("service.").split(".")[0] in {"accounts", "of_client", "auth", "db"}:
            raise AssertionError("legacy dependency executed before role refusal")
sys.meta_path.insert(0,NoLegacyDependencies())
try:
    import server
except RuntimeError as exc:
    assert str(exc) == "Use onlyfans_app:app for the OnlyFans API role; legacy relay startup is refused"
else:
    raise AssertionError("legacy server import was permitted")
"""
    env = {**os.environ, "ONLYFANS_PROCESS_ROLE": role}
    result = subprocess.run([sys.executable, "-c", script, str(_SERVICE)], env=env,
                            text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr or result.stdout


def test_raw_http_mutation_sinks_require_review():
    """An added verb/raw session needs an explicit isolation test and review."""
    tree = ast.parse((_SERVICE / "of_client.py").read_text(encoding="utf-8"))
    writes, sessions = set(), set()

    class Sinks(ast.NodeVisitor):
        def __init__(self):
            self.scope = []

        def visit_FunctionDef(self, node):
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_Call(self, node):
            if isinstance(node.func, ast.Attribute):
                receiver = ast.unparse(node.func.value)
                if receiver != "self" and node.func.attr in {"post", "put", "patch", "delete", "request"}:
                    writes.add((".".join(self.scope), ast.unparse(node.func)))
                if node.func.attr == "Session":
                    sessions.add((".".join(self.scope), ast.unparse(node.func)))
            self.generic_visit(node)

    Sinks().visit(tree)
    assert writes == {
        ("_http_call._do", "self.http.request"),
        ("convert_register", "self.http.post"),
        ("upload_media._put", "self.http.put"),
        ("upload_media._put_part", "sess.put"),
    }
    assert sessions == {("__init__", "requests.Session"), ("_upload_part_session", "requests.Session")}
