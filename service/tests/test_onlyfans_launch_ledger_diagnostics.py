"""Redaction and read-only recovery diagnostics with synthetic data only.

The collector tests use an isolated temporary SQLite database and the actual
Store._runtime access checks. No provider or credential source is contacted.
"""
from datetime import datetime, timedelta
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from db.models import Account, Base, OfCreatorBinding
from db.onlyfans_models import OfRuntime
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_ledger_models import OfLedgerScan
from onlyfans_read.agency_scope import AgencyScope, current_scope
from onlyfans_runtime import connections, ledger, provider_connector
from onlyfans_runtime.launch_ledger.diagnostics import collect_diagnostic, inspect_page, SCHEMA
from onlyfans_runtime.native_protocol import NativeProtocolError
from onlyfans_runtime.store import Store


START, END = datetime(2026, 10, 1), datetime(2026, 10, 20)
OWNER, FAN = 7001, 101
TENANT, ACCOUNT, BINDING = "synthetic-tenant", "synthetic-account", "synthetic-binding"
SECRET = "private-secret-marker"


def transaction(**changes):
    row = dict(id="synthetic-payment", ownerId=OWNER, user={"id": FAN}, amount="10.01",
        currency="USD", status="done", createdAt="2026-10-18T09:00:00Z",
        description="Payment for message from " + SECRET)
    row.update(changes)
    return row


def payload(rows, more=False, **extra):
    return json.dumps(dict(list=rows, hasMore=more, **extra), ensure_ascii=True).encode()


def inspect(raw):
    return inspect_page(raw, owner_id=OWNER, start=START, end=END)


def test_valid_structure_returns_no_values_unknown_names_or_private_identifiers():
    row = transaction(**{SECRET: {"password": SECRET}, "user": {"id": FAN, SECRET: SECRET}})
    result = inspect(payload([row], **{SECRET: SECRET}))
    assert result["schema"] == SCHEMA and result["valid"] is True and result["applied"] is False
    assert result["error_code"] is None and result["issues"] == []
    assert result["structure"]["unknown_field_count"] == 1
    shape = result["structure"]["rows"][0]
    assert shape["unknown_field_count"] == shape["user_unknown_field_count"] == 1
    assert shape["fields"]["amount"] == {"present": True, "type": "string"}
    assert shape["fields"]["messageId"] == {"present": False, "type": None}
    rendered = json.dumps(result)
    for forbidden in (SECRET, str(OWNER), str(FAN), "synthetic-payment", "2026-10-18", "10.01", "USD"):
        assert forbidden not in rendered


def test_each_bad_row_uses_real_parser_and_reports_bounded_row_position():
    result = inspect(payload([transaction(id="one", amount="1.005"),
                              transaction(id="two", ownerId=OWNER + 1),
                              transaction(id="three", currency="EUR")]))
    assert result["valid"] is False and result["applied"] is False
    assert result["issues"] == [
        {"code": "invalid_amount", "path": "amount", "row_index": 0},
        {"code": "owner_mismatch", "path": "ownerId", "row_index": 1},
        {"code": "invalid_currency", "path": "currency", "row_index": 2},
    ]


def test_complete_page_duplicate_check_is_retained():
    result = inspect(payload([transaction(), transaction()]))
    assert result["valid"] is False
    assert result["issues"] == [{"code": "duplicate_provider_id", "path": "id", "row_index": 1}]


def test_decimal_tokens_are_never_rounded_when_diagnosing_other_rows():
    raw = payload([transaction(id="precise", amount="0.100000000000000000001"),
                   transaction(id="second", currency="EUR")])
    raw = raw.replace(b'"0.100000000000000000001"', b'0.100000000000000000001')
    result = inspect(raw)
    assert result["structure"]["rows"][0]["fields"]["amount"]["type"] == "decimal"
    assert {issue["code"] for issue in result["issues"]} == {"invalid_amount", "invalid_currency"}


@pytest.mark.parametrize("raw", [b" " * 65537, b'{"list":[],"list":[],"hasMore":false}',
    b'{"list":[],"hasMore":NaN}', b"[1,2]", b"{", b"\xff"],
    ids=["oversized", "duplicate-key", "nonfinite", "array-root", "truncated", "invalid-utf8"])
def test_invalid_json_and_size_have_no_structure_and_no_relaxed_loader(raw):
    result = inspect(raw)
    assert result["valid"] is False and result["structure"] is None
    assert len(result["issues"]) == 1
    assert result["error_code"] in {"invalid_json", "invalid_response_size"}


@pytest.mark.parametrize("raw,code", [(payload([], True), "empty_nonterminal_page"),
    (b'{"list":{},"hasMore":false}', "invalid_page_list"),
    (b'{"list":[],"hasMore":"secret"}', "invalid_has_more")])
def test_page_frame_failure_is_diagnosed_before_rows(raw, code):
    result = inspect(raw)
    assert result["error_code"] == code and len(result["issues"]) == 1


def test_row_and_issue_counts_stay_bounded():
    result = inspect(payload([transaction(id=str(index), amount=True) for index in range(20)]))
    assert len(result["issues"]) == len(result["structure"]["rows"]) == 20
    assert result["issues_truncated"] is False
    oversized = inspect(payload([transaction(id=str(index)) for index in range(21)]))
    assert len(oversized["structure"]["rows"]) == 20
    assert oversized["structure"]["rows_truncated"] is True
    assert oversized["error_code"] == "invalid_page_list"


def test_exception_strings_cannot_escape_the_diagnostic(monkeypatch):
    def fail(*args, **kwargs):
        raise NativeProtocolError(SECRET)
    monkeypatch.setattr(ledger, "parse_page", fail)
    result = inspect(payload([transaction()]))
    assert result["error_code"] == "validation_failed"
    assert SECRET not in json.dumps(result)


@pytest.fixture
def scope(tmp_path, monkeypatch):
    from onlyfans_runtime.launch_ledger import diagnostics
    now = datetime(2026, 10, 23, 9)
    monkeypatch.setattr(diagnostics, "utcnow", lambda: now)
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    engine = create_engine("sqlite+pysqlite:///" + str(tmp_path / "diagnostic.db"))
    models = (Account, OfCreatorBinding, OfRuntime, OfProviderConnection, OfLedgerScan)
    Base.metadata.create_all(engine, tables=[model.__table__ for model in models])
    with Session(engine) as session, session.begin():
        session.add(Account(id=ACCOUNT, nickname="Synthetic"))
        session.add(OfCreatorBinding(id=BINDING, tenant_id=TENANT, creator_id="synthetic-creator",
            creator_name="Synthetic", account_id=ACCOUNT, handle="synthetic", channel="onlyfans"))
        session.add(OfRuntime(account_id=ACCOUNT, binding_id=BINDING, enabled=False, activated_at=START))
        session.add(OfProviderConnection(account_id=ACCOUNT, binding_id=BINDING, owner_id=OWNER,
            revision=1, sealed_config=SECRET, expires_at=now + timedelta(hours=1), status="paused",
            verified_at=now - timedelta(hours=1), updated_at=now))
        session.add(OfLedgerScan(account_id=ACCOUNT, binding_id=BINDING, connection_revision=1,
            owner_id=OWNER, window_start=START, window_end=END, page_offset=40, status="held",
            next_poll_at=now + timedelta(minutes=5), last_error="unsupported_ledger_response", updated_at=now))
    store = SimpleNamespace(engine=engine, _runtime=Store._runtime)
    calls, statements = [], []
    def before_execute(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", before_execute)
    clear = {"capture": {"synthetic": SECRET}, "xHash": SECRET, "proxyUrl": "http://" + SECRET + ".invalid"}
    monkeypatch.setattr(connections, "_open", lambda connection, tenant: clear)
    monkeypatch.setattr(provider_connector, "session_from_capture", lambda **kwargs: kwargs)
    behavior = SimpleNamespace(raw=payload([transaction()]), callback=None, exception=None)
    class Transport:
        def __init__(self, handle):
            assert handle["account_id"] == ACCOUNT and handle["owner_id"] == OWNER
            assert handle["capture"] is clear["capture"]
        def transaction_page(self, **kwargs):
            assert engine.pool.checkedout() == 0, "provider read must be outside DB transaction"
            calls.append(kwargs)
            if behavior.callback:
                behavior.callback()
            if behavior.exception:
                raise behavior.exception
            return behavior.raw
    monkeypatch.setattr(provider_connector, "NativeProviderConnector", Transport)
    yield SimpleNamespace(store=store, engine=engine, calls=calls, statements=statements,
                          behavior=behavior, now=now, models=models)
    engine.dispose()


def snapshot(scope):
    with scope.engine.connect() as connection:
        return {model.__tablename__: [tuple(row) for row in connection.execute(select(model.__table__))]
                for model in scope.models}


def test_collector_reads_one_exact_held_page_and_cannot_change_state(scope):
    before = snapshot(scope)
    result = collect_diagnostic(scope.store, TENANT, ACCOUNT)
    assert result["valid"] is True and result["applied"] is False
    assert result["collection"] == {"attempted": True, "snapshot_unchanged": True}
    assert scope.calls == [{"offset": 40, "limit": 20, "start": START, "end": END}]
    assert snapshot(scope) == before
    assert all(statement.lstrip().split()[0].upper() not in {"INSERT", "UPDATE", "DELETE"}
               for statement in scope.statements)
    for private in (SECRET, ACCOUNT, TENANT, BINDING, str(OWNER), str(FAN)):
        assert private not in json.dumps(result)


@pytest.mark.parametrize("model,changes,code", [
    (OfLedgerScan, {"status": "running"}, "scan_busy"),
    (OfLedgerScan, {"lease_token": "synthetic-token", "lease_until": END}, "scan_busy"),
    (OfLedgerScan, {"status": "idle"}, "held_scan_required"),
    (OfLedgerScan, {"last_error": "other"}, "held_scan_required"),
    (OfLedgerScan, {"page_offset": 20}, "scan_offset_changed"),
    (OfLedgerScan, {"connection_revision": 2}, "connection_unavailable"),
    (OfLedgerScan, {"owner_id": OWNER + 1}, "connection_unavailable"),
    (OfLedgerScan, {"window_start": END + timedelta(seconds=1)}, "scan_window_invalid"),
    (OfProviderConnection, {"expires_at": END}, "connection_unavailable"),
    (OfProviderConnection, {"status": "failed"}, "connection_unavailable"),
    (OfProviderConnection, {"verified_at": None}, "connection_unavailable"),
])
def test_ineligible_scan_or_connection_never_reaches_provider(scope, model, changes, code):
    with Session(scope.engine) as session, session.begin():
        row = session.get(model, ACCOUNT)
        for key, value in changes.items():
            setattr(row, key, value)
    before = snapshot(scope)
    result = collect_diagnostic(scope.store, TENANT, ACCOUNT)
    assert result["error_code"] == code and result["collection"]["attempted"] is False
    assert result["structure"] is None and scope.calls == [] and snapshot(scope) == before


def test_missing_scan_never_creates_one(scope):
    with Session(scope.engine) as session, session.begin():
        session.delete(session.get(OfLedgerScan, ACCOUNT))
    assert collect_diagnostic(scope.store, TENANT, ACCOUNT)["error_code"] == "held_scan_required"
    assert not scope.calls
    with Session(scope.engine) as session:
        assert session.get(OfLedgerScan, ACCOUNT) is None


@pytest.mark.parametrize("tenant,account", [("other-tenant", ACCOUNT), (TENANT, "other-account")])
def test_tenant_and_account_isolation(scope, tenant, account):
    result = collect_diagnostic(scope.store, tenant, account)
    assert result["error_code"] == "diagnostic_scope_unavailable" and not scope.calls


def test_existing_agency_creator_scope_is_enforced(scope):
    token = current_scope.set(AgencyScope(("another-creator",), "synthetic-actor", True))
    try:
        result = collect_diagnostic(scope.store, TENANT, ACCOUNT)
    finally:
        current_scope.reset(token)
    assert result["error_code"] == "diagnostic_scope_unavailable" and not scope.calls


@pytest.mark.parametrize("model,changes", [
    (OfProviderConnection, {"revision": 2}),
    (OfProviderConnection, {"expires_at": END}),
    (OfProviderConnection, {"status": "verified"}),
    (OfProviderConnection, {"sealed_config": "replacement-synthetic-secret"}),
    (OfLedgerScan, {"status": "running"}),
    (OfLedgerScan, {"page_offset": 60}),
    (OfLedgerScan, {"window_end": END - timedelta(seconds=1)}),
    (OfLedgerScan, {"updated_at": END}),
    (OfCreatorBinding, {"revoked_at": END}),
    (OfCreatorBinding, {"tenant_id": "other-tenant"}),
])
def test_changed_state_discards_the_response(scope, model, changes):
    def change():
        with Session(scope.engine) as session, session.begin():
            row = session.get(model, BINDING if model is OfCreatorBinding else ACCOUNT)
            for key, value in changes.items():
                setattr(row, key, value)
    scope.behavior.callback = change
    result = collect_diagnostic(scope.store, TENANT, ACCOUNT)
    assert result["error_code"] == "diagnostic_state_changed"
    assert result["structure"] is None and result["issues"] == []
    assert result["collection"] == {"attempted": True, "snapshot_unchanged": False}
    assert len(scope.calls) == 1


def test_transport_error_is_redacted_and_never_retried(scope):
    before = snapshot(scope)
    scope.behavior.exception = RuntimeError(SECRET)
    result = collect_diagnostic(scope.store, TENANT, ACCOUNT)
    assert result["error_code"] == "diagnostic_read_failed" and len(scope.calls) == 1
    assert SECRET not in json.dumps(result) and snapshot(scope) == before


@pytest.mark.parametrize("offset", [True, -1, 10001, "40"])
def test_invalid_offset_never_opens_a_connection(scope, offset):
    assert collect_diagnostic(scope.store, TENANT, ACCOUNT, expected_offset=offset)["error_code"] == "invalid_diagnostic_scope"
    assert not scope.calls


def test_wrong_process_role_cannot_read_provider(scope, monkeypatch):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "api")
    assert collect_diagnostic(scope.store, TENANT, ACCOUNT)["error_code"] == "invalid_diagnostic_scope"
    assert not scope.calls
